import sys
sys.path.append('../input/pytorch-image-models/pytorch-image-models-master')
import os
import gc
import re
import math
import time
import random
import shutil
import pickle
from pathlib import Path
from contextlib import contextmanager
from collections import defaultdict, Counter

import scipy as sp
import numpy as np
import pandas as pd
from tqdm.auto import tqdm

import Levenshtein
from sklearn import preprocessing
from sklearn.model_selection import StratifiedKFold, GroupKFold, KFold

from functools import partial

import cv2
from PIL import Image

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import Adam, SGD
import torchvision.models as models
from torch.nn.parameter import Parameter
from torch.utils.data import DataLoader, Dataset
from torch.nn.utils.rnn import pad_sequence, pack_padded_sequence
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts, CosineAnnealingLR, ReduceLROnPlateau


import warnings 
warnings.filterwarnings('ignore')

from preprocessing import Tokenizer, TestDataset
from model import Encoder, DecoderWithAttention
from utils import get_test_file_path, get_train_file_path, init_logger, seed_torch, get_score, get_transforms

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


# Load Tokenizer
tokenizer = torch.load('../input/inchi-preprocess-2/tokenizer2.pth')
print(f"tokenizer.stoi: {tokenizer.stoi}")

# ====================================================
# CFG
# ====================================================
class CFG:
    debug=False
    max_len=275
    print_freq=1000
    num_workers=4
    model_name='resnet34'
    size=224
    scheduler='CosineAnnealingLR' # ['ReduceLROnPlateau', 'CosineAnnealingLR', 'CosineAnnealingWarmRestarts']
    epochs=1 # not to exceed 9h
    #factor=0.2 # ReduceLROnPlateau
    #patience=4 # ReduceLROnPlateau
    #eps=1e-6 # ReduceLROnPlateau
    T_max=4 # CosineAnnealingLR
    #T_0=4 # CosineAnnealingWarmRestarts
    encoder_lr=1e-4
    decoder_lr=4e-4
    min_lr=1e-6
    batch_size=64
    weight_decay=1e-6
    gradient_accumulation_steps=1
    max_grad_norm=5
    attention_dim=256
    embed_dim=256
    decoder_dim=512
    dropout=0.5
    seed=42
    n_fold=5
    trn_fold=[0] # [0, 1, 2, 3, 4]
    train=True

LOGGER = init_logger()
seed_torch(seed=CFG.seed)

# ====================================================
# Inference
# ====================================================
def inference(test_loader, encoder, decoder, tokenizer, device):
    encoder.eval()
    decoder.eval()
    text_preds = []
    tk0 = tqdm(test_loader, total=len(test_loader))
    for images in tk0:
        images = images.to(device)
        with torch.no_grad():
            features = encoder(images)
            predictions = decoder.predict(features, CFG.max_len, tokenizer)
        predicted_sequence = torch.argmax(predictions.detach().cpu(), -1).numpy()
        _text_preds = tokenizer.predict_captions(predicted_sequence)
        text_preds.append(_text_preds)
    text_preds = np.concatenate(text_preds)
    return text_preds

# test = pd.read_csv('../input/bms-molecular-translation/sample_submission.csv')
# test['file_path'] = test['image_id'].apply(get_test_file_path)
# print(f'test.shape: {test.shape}')

train = pd.read_csv('../input/bms-molecular-translation/train_labels.csv')
train_samples = train.sample(n=10000)
train_samples['file_path'] = train_samples['image_id'].apply(get_train_file_path)
print(f'train_samples.shape: {train_samples.shape}')

states = torch.load(f'../input/inchi-resnet-lstm-with-attention-starter/{CFG.model_name}_fold0_best.pth', map_location=torch.device('cpu'))

encoder = Encoder(CFG.model_name, pretrained=False)
encoder.load_state_dict(states['encoder'])
encoder.to(device)
decoder = DecoderWithAttention(attention_dim=CFG.attention_dim,
                               embed_dim=CFG.embed_dim,
                               decoder_dim=CFG.decoder_dim,
                               vocab_size=len(tokenizer),
                               dropout=CFG.dropout,
                               device=device)
decoder.load_state_dict(states['decoder'])
decoder.to(device)
del states; gc.collect()
test_dataset = TestDataset(train_samples, transform=get_transforms(CFG.size, data='valid'))
test_loader = DataLoader(test_dataset, batch_size=512, shuffle=False, num_workers=CFG.num_workers)
predictions = inference(test_loader, encoder, decoder, tokenizer, device)
del test_loader, encoder, decoder, tokenizer; gc.collect()

# # submission
# test['InChI'] = [f"InChI=1S/{text}" for text in predictions]
# test[['image_id', 'InChI']].to_csv('submission.csv', index=False)
train_samples['InChI_Predict'] = [f"InChI=1S/{text}" for text in predictions.tolist()]
train_samples[['image_id', 'InChI', 'InChI_Predict']].to_csv('../output/submission_train.csv', index=False)

avg_score = get_score(train_samples['InChI'].values.tolist(), train_samples['InChI_Predict'].values.tolist())
print('avg score:{}'.format(avg_score))

label_parts_predict = train_samples['InChI_Predict'].map(lambda x: x.split('/'))
df_predict = pd.DataFrame.from_records(label_parts_predict.values)
train_samples['InChI_Predict_part2'] = df_predict[0] + '/' + df_predict[1]

label_parts_true = train_samples['InChI_Predict'].map(lambda x: x.split('/'))
df_true = pd.DataFrame.from_records(label_parts_true.values)
train_samples['InChI_part2'] = df_true[0] + '/' + df_true[1]

avg_score_part2 = get_score(train_samples['InChI'].values.tolist(), train_samples['InChI_Predict'].values.tolist())
print('avg score part2:{}'.format(avg_score_part2))