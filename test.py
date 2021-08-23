# %load evaluate_eer.py
# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import os

import numpy as np
import paddle
import paddle.nn as nn
import paddle.nn.functional as F
import paddleaudio
import yaml
from paddleaudio.transforms import *
from paddleaudio.utils import get_logger

import metrics
from dataset import get_val_loader
from models import ECAPA_TDNN, ResNetSE34, ResNetSE34V2

logger = get_logger()

file2feature = {}


class Normalize:
    def __init__(self, eps=1e-8):
        self.eps = eps

    def __call__(self, x):
        assert x.ndim == 3
        mean = paddle.mean(x, [1, 2], keepdim=True)
        std = paddle.std(x, [1, 2], keepdim=True)
        return (x - mean) / (std + self.eps)


def get_feature(file, model, melspectrogram, random_sampling=True):
    global file2feature
    if file in file2feature:
        return file2feature[file]
    s0, _ = paddleaudio.load(file, sr=16000)  #, norm_type='gaussian')
    rs_duration = 6
    if len(s0) > rs_duration * 16000 and random_sampling:
        wavs = []
        k = 8
        pos = np.linspace(0,
                          len(s0) - rs_duration * 16000,
                          num=k,
                          dtype='int32')
        for i in pos:
            s = s0[i:i + int(rs_duration * 16000)]
            if np.sum(np.abs(s) < 0.01) > rs_duration * 16000 * 0.5:
                print('sound ignored')
                continue
            s = paddle.to_tensor(s[None, :])
            wavs += [s]
        if len(wavs) == 0:
            features = paddle.randn((1, 256))
        else:
            x = melspectrogram(paddle.concat(wavs, 0)).astype('float32')
            with paddle.no_grad():
                features = model(x)  #.squeeze()
            features = paddle.nn.functional.normalize(features, axis=-1)
    else:
        features = []
        s = paddle.to_tensor(s0[None, :])
        s = melspectrogram(s).astype('float32')
        with paddle.no_grad():
            feature = model(s)  #.squeeze()
        feature = feature / paddle.sqrt(paddle.sum(feature**2))
        features += [feature]

    file2feature.update({file: features})
    return features


class Normalize2:
    def __init__(self, mean_file, eps=1e-5):
        self.eps = eps
        mean = paddle.load(mean_file)['mean']
        std = paddle.load(mean_file)['std']

        self.mean = mean.unsqueeze((0, 2))

    def __call__(self, x):
        assert x.ndim == 3
        return x - self.mean


def get_score2(features1, features2):
    scores = []
    for f1 in features1:
        for f2 in features2:
            scores += [float(paddle.dot(f1.squeeze(), f2.squeeze()))]
    return np.mean(scores)  #/(0.000001+np.std(scores))


def get_score3(features1, features2):
    if isinstance(features1, list):
        features1 = paddle.concat(features1)
    if isinstance(features2, list):
        features2 = paddle.concat(features2)
    f1 = paddle.mean(features1, 0)
    f2 = paddle.mean(features2, 0)
    score = float(paddle.dot(f1.squeeze(), f2.squeeze()))
    return score


def get_score4(features1, features2):  # similarity mean
    if isinstance(features1, list):
        features1 = paddle.concat(features1)
    if isinstance(features2, list):
        features2 = paddle.concat(features2)
    sim = features1 @ features2.t()
    score = float(paddle.max(sim))
    return score


def get_score(features1, features2):  # feature mean
    if isinstance(features1, list):
        features1 = paddle.concat(features1)
    if isinstance(features2, list):
        features2 = paddle.concat(features2)
    m1 = paddle.mean(features1, 0)
    m2 = paddle.mean(features2, 0)
    m1 = m1 / paddle.sqrt(paddle.sum(m1**2))
    m2 = m2 / paddle.sqrt(paddle.sum(m2**2))
    score = float(paddle.dot(m1.squeeze(), m2.squeeze()))
    return score


def compute_eer(config, model):

    transforms = []
    melspectrogram = LogMelSpectrogram(**config['fbank'])
    transforms += [melspectrogram]
    if config['normalize']:
        transforms += [Normalize2(config['mean_std_file'])]

    transforms = Compose(transforms)

    global file2feature
    file2feature = {}
    test_list = config['test_list']
    test_folder = config['test_folder']
    model.eval()
    with open(test_list) as f:
        lines = f.read().split('\n')
    label_wav_pairs = [l.split() for l in lines if len(l) > 0]
    logger.info(f'{len(label_wav_pairs)} test pairs listed')
    labels = []
    scores = []
    for i, (label, f1, f2) in enumerate(label_wav_pairs):
        full_path1 = os.path.join(test_folder, f1)
        full_path2 = os.path.join(test_folder, f2)
        feature1 = get_feature(full_path1, model, transforms)
        feature2 = get_feature(full_path2, model, transforms)
        score = get_score(feature1, feature2)
        #  +get_score(feature1,feature2)#float(paddle.dot(feature1.squeeze(), feature2.squeeze()))
        labels.append(label)
        scores.append(score)
        if i % (len(label_wav_pairs) // 10) == 0:
            logger.info(f'processed {i}|{len(label_wav_pairs)}')

    scores = np.array(scores)
    labels = np.array([int(l) for l in labels])
    result = metrics.compute_eer(scores, labels)
    min_dcf = metrics.compute_min_dcf(result.fr, result.fa)
    logger.info(f'eer={result.eer}, thresh={result.thresh}, minDCF={min_dcf}')
    return result, min_dcf


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('-c',
                        '--config',
                        type=str,
                        required=False,
                        default='config.yaml')
    parser.add_argument(
        '-d',
        '--device',
        default="gpu",
        help="Select which device to train model, defaults to gpu.")
    parser.add_argument('-w', '--weight', type=str, required=True)
    # parser.add_argument('--test_list', type=str, required=True)
    # parser.add_argument('--test_folder', type=str, required=True)
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)
    paddle.set_device(args.device)
    logger.info('model:' + config['model']['name'])
    logger.info('device: ' + args.device)

    logger.info(f'using ' + config['model']['name'])
    ModelClass = eval(config['model']['name'])
    model = ModelClass(**config['model']['params'])
    state_dict = paddle.load(args.weight)
    if 'model' in state_dict.keys():
        state_dict = state_dict['model']

    model.load_dict(state_dict)
    result, min_dcf = compute_eer(config, model)
    logger.info(f'eer={result.eer}, thresh={result.thresh}, minDCF={min_dcf}')
