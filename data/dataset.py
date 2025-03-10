import json
import os
from copy import deepcopy
from operator import itemgetter
import pandas as pd
import albumentations as A
import numpy as np
from PIL import Image
from tokenizers import AddedToken
from torch.utils.data import Dataset
from transformers import Pix2StructProcessor
import io
import random
from utils.constant import TOKEN_MAP


def tokenize_dict(data: dict):
    """
    Convert a nested JSON object into a token sequence and count tokens dynamically using JSON tags.
    
    Args:
    - data (dict): The input dictionary (e.g., JSON object).
    
    Returns:
    - token_count (dict): A dictionary containing the count of each generated token.
    - token_sequence (str): The tokenized sequence as a string.
    """

    def recursive_tokenizer(d):
        if isinstance(d, dict):
            result = ""
            for key, value in d.items():
                start_token = f"<{key}>"
                end_token = f"</{key}>"
                value_string = recursive_tokenizer(value)
                result += f"{start_token}{value_string}{end_token}"
            return result
        elif isinstance(d, list):  
            try:
                if isinstance(d[0], dict) and (('x' in d[0] and 'y' in d[0]) or 'text' in d[0]):
                    return ''.join(recursive_tokenizer(item) for item in d)
                return ' '.join(recursive_tokenizer(item) for item in d)
            except Exception as e:
                ...
        else:
            if type(d) == float:
                return str(round(d, 2))
            return str(d)
    token_sequence = recursive_tokenizer(data)
    return  token_sequence

def get_processor(cfg):
    """
    load the processor
    """
    processor_path = cfg.model.backbone_path
    print(f"loading processor from {processor_path}")
    processor = Pix2StructProcessor.from_pretrained(processor_path)
    processor.image_processor.is_vqa = False
    processor.image_processor.patch_size = {
        "height": cfg.model.patch_size,
        "width": cfg.model.patch_size
    }
    print("adding new tokens...")
    new_tokens = []
    for _, this_tok in TOKEN_MAP.items():
        for tok in this_tok:
            new_tokens.append(tok)
    new_tokens = sorted(new_tokens)
    tokens_to_add = []
    for this_tok in new_tokens:
        tokens_to_add.append(AddedToken(this_tok, lstrip=False, rstrip=False))
    processor.tokenizer.add_tokens(tokens_to_add)
    return processor

class ChartDataset(Dataset):
    def __init__(self, cfg, parquet_path, sharing = None, selected_ids_for_valid  = None):
        self.cfg = cfg
        self.resize_height = cfg.images.rsz_height
        self.resize_width = cfg.images.rsz_width
        self.transform = create_train_transforms(self.resize_height, self.resize_width)
        self.parquet_df = pd.read_parquet(parquet_path)
        self.graph_ids = self.parquet_df.index.tolist()
        #Exclude the validation datas
        if selected_ids_for_valid:
            self.graph_ids = [id for id in self.graph_ids if id not in selected_ids_for_valid]
        else:
            #If not then don't exclude
            self.selected_ids_for_valid = []
        #Share with validation with a percentage of training set
        if sharing:
            print(f"Validation parquet not found, selecting randomly from training set: {sharing*100}")
            num_to_select = int(len(self.graph_ids) * (sharing))
            self.graph_ids = random.sample(self.graph_ids, num_to_select)
            self.selected_ids_for_valid = self.graph_ids
        self.load_processor()
    def share_validation(self):
        return self.selected_ids_for_valid
    def load_processor(self):
        self.processor = get_processor(self.cfg)

    def load_image(self, graph_id):
        row = self.parquet_df.loc[graph_id]
        image_data = row["image"]["bytes"]  
        image = Image.open(io.BytesIO(image_data)).convert('RGB')  #
        return image

    def build_output(self, graph_id):
        row = self.parquet_df.loc[graph_id]
        
        try:
            ground_truth_str = row["annotation"]
            ground_truth = json.loads(ground_truth_str)  
            
            chart_type = ground_truth.get('chart_type', 'unknown')  
            
            text = tokenize_dict(ground_truth)  

            
            # e_string = self.processor.tokenizer.eos_token
            res_text = f"{text}"
            return res_text, chart_type
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON for graph_id {graph_id}: {e}")
            return 'error', 'error_chart'
        except Exception as e:
            print(f"Error building output for graph_id {graph_id}: {e}")
            return 'error', 'error_chart'

    def __len__(self):
        return len(self.graph_ids)

    def __getitem__(self, index):
        graph_id = self.graph_ids[index]
        image = self.load_image(graph_id)

        if self.transform:
            image = np.array(image)
            image = self.transform(image=image)["image"]

        try:
            text, chart_type = self.build_output(graph_id)
        except Exception as e:
            print(f"Error in {graph_id}: {e}")
            text, chart_type = 'error', 'error_chart'

        # Process the image using the processor
        p_img = self.processor(
            images=image,
            max_patches=self.cfg.model.max_patches,
            add_special_tokens=True,
        )

        # Process the text using the processor
        p_txt = self.processor(
            text=text,
            truncation=True,
            add_special_tokens=True,
            max_length=self.cfg.model.max_length,
        )

        # Constructing the output dictionary
        r = {
            'id': graph_id,
            'chart_type': chart_type,
            'image': image,
            'text': text,
            'flattened_patches': p_img['flattened_patches'],
            'attention_mask': p_img['attention_mask'],
        }

        # Safely handling decoder input ids and attention masks
        r['decoder_input_ids'] = p_txt.get('decoder_input_ids', p_txt.get('input_ids', []))
        r['decoder_attention_mask'] = p_txt.get('decoder_attention_mask', p_txt.get('attention_mask', []))

        return r

def create_train_transforms(resize_height=None, resize_width=None):
    """
    Returns transformations.

    Returns:
        albumentations transforms: transforms.
    """
    transform_lists = []
    if resize_height is not None and resize_width is not None:
        transform_lists.append(A.Resize(height=resize_height, width=resize_width))
    transforms = A.Compose(transform_lists)
    return transforms
