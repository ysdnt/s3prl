# -*- coding: utf-8 -*- #
"""*********************************************************************************************"""
#   FileName     [ expert.py ]
#   Synopsis     [ the phone linear downstream wrapper ]
#   Author       [ S3PRL ]
#   Copyright    [ Copyleft(c), Speech Lab, NTU, Taiwan ]
"""*********************************************************************************************"""


###############
# IMPORTATION #
###############
import os
import math
import torch
import random
import pathlib
from pathlib import Path
from argparse import Namespace
from time import sleep
#-------------#
import torch
import numpy as np
import torch.nn as nn
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader, DistributedSampler
from torch.distributed import is_initialized, get_rank, get_world_size
#-------------#
from s3prl.utility.helper import is_leader_process
from .model import Model, AMSoftmaxLoss, AAMSoftmaxLoss, SoftmaxLoss, UtteranceExtractor
from .dataset import SpeakerVerifi_train, SpeakerVerifi_test
from .utils import EER


class DownstreamExpert(nn.Module):
    """
    Used to handle downstream-specific operations
    eg. downstream forward, metric computation, contents to log

    Note 1.
        dataloaders should output in the following format:

        [[wav1, wav2, ...], your_other_contents, ...]

        where wav1, wav2 ... are in variable length
        and wav1 is in torch.FloatTensor
    """

    def __init__(self, upstream_dim, downstream_expert, expdir, **kwargs):
        super(DownstreamExpert, self).__init__()
        # config
        self.upstream_dim = upstream_dim
        self.downstream = downstream_expert
        self.datarc = downstream_expert['datarc']
        self.modelrc = downstream_expert['modelrc']
        self.expdir = expdir

        # dataset
        # train_file_path = Path(self.datarc['file_path']) / "train"
        # test_file_path = Path(self.datarc['file_path']) / "val"

        train_file_path_voxceleb = Path(self.datarc['file_path']) / "voxceleb1" / "vox1_dev_wav" / "wav"
        train_file_path_zalo = Path(self.datarc['file_path']) / "zalo-sv" / "zalo_dataset" / "train"
        test_file_path = Path(self.datarc['file_path']) / "zalo-sv" / "zalo_dataset" / "val"

        #test_file_path = "/content/gdrive/MyDrive/KLTN/dataset/zalo_dataset/dataset_fix/val"
        #train_file_path = Path(self.datarc['file_path']) / "train"
        #test_file_path = Path(self.datarc['file_path']) / "train"
        
        # train_config = {
        #     "vad_config": self.datarc['vad_config'],
        #     "file_path": [train_file_path],
        #     "key_list": ["Voxceleb1"],
        #     "meta_data": self.datarc['train_meta_data'],
        #     "max_timestep": self.datarc["max_timestep"],
        # }
        train_config = {
            "vad_config": self.datarc['vad_config'],
            "file_path": [train_file_path_voxceleb, train_file_path_zalo],
            "key_list": ["Voxceleb1", "Zalo"],
            "meta_data": self.datarc['train_meta_data'],
            "max_timestep": self.datarc["max_timestep"],
        }
        self.train_dataset = SpeakerVerifi_train(**train_config)

        # dev_config = {
        #     "vad_config": self.datarc['vad_config'],
        #     "file_path": train_file_path, 
        #     "meta_data": self.datarc['dev_meta_data']
        # }
        dev_config = {
            "vad_config": self.datarc['vad_config'],
            "file_path": test_file_path, 
            "meta_data": self.datarc['dev_meta_data']
        }           
        self.dev_dataset = SpeakerVerifi_test(**dev_config)

        test_config = {
            "vad_config": self.datarc['vad_config'],
            "file_path": test_file_path, 
            "meta_data": self.datarc['test_meta_data']
        }
        self.test_dataset = SpeakerVerifi_test(**test_config)

        # module
        self.connector = nn.Linear(self.upstream_dim, self.modelrc['input_dim'])

        # downstream model
        agg_dim = self.modelrc["module_config"][self.modelrc['module']].get(
            "agg_dim",
            self.modelrc['input_dim']
        )
        
        ModelConfig = {
            "input_dim": self.modelrc['input_dim'],
            "agg_dim": agg_dim,
            "agg_module_name": self.modelrc['agg_module'],
            "module_name": self.modelrc['module'], 
            "hparams": self.modelrc["module_config"][self.modelrc['module']],
            "utterance_module_name": self.modelrc["utter_module"]
        }
        # downstream model extractor include aggregation module
        self.model = Model(**ModelConfig)


        # SoftmaxLoss or AMSoftmaxLoss
        objective_config = {
            "speaker_num": self.train_dataset.speaker_num, 
            "hidden_dim": self.modelrc['input_dim'], 
            **self.modelrc['LossConfig'][self.modelrc['ObjectiveLoss']]
        }

        self.objective = eval(self.modelrc['ObjectiveLoss'])(**objective_config)
        # utils
        self.score_fn  = nn.CosineSimilarity(dim=-1)
        self.eval_metric = EER
        self.register_buffer('best_score', torch.ones(1) * 100)

    # Interface
    def get_dataloader(self, mode):
        """
        Args:
            mode: string
                'train', 'dev' or 'test'

        Return:
            a torch.utils.data.DataLoader returning each batch in the format of:

            [wav1, wav2, ...], your_other_contents1, your_other_contents2, ...

            where wav1, wav2 ... are in variable length
            each wav is torch.FloatTensor in cpu with:
                1. dim() == 1
                2. sample_rate == 16000
                3. directly loaded by torchaudio
        """

        if mode == 'train':
            return self._get_train_dataloader(self.train_dataset)            
        elif mode == 'dev':
            return self._get_eval_dataloader(self.dev_dataset)
        elif mode == 'test':
            return self._get_eval_dataloader(self.test_dataset)

    def _get_train_dataloader(self, dataset):
        print(Path(self.datarc['file_path']))
        sampler = DistributedSampler(dataset) if is_initialized() else None
        return DataLoader(
            dataset,
            batch_size=self.datarc['train_batch_size'], 
            shuffle=(sampler is None),
            sampler=sampler,
            num_workers=self.datarc['num_workers'],
            collate_fn=dataset.collate_fn
        )

    def _get_eval_dataloader(self, dataset):
        return DataLoader(
            dataset, batch_size=self.datarc['eval_batch_size'],
            shuffle=False, num_workers=self.datarc['num_workers'],
            collate_fn=dataset.collate_fn
        )

    # Interface
    def get_train_dataloader(self):
        return self._get_train_dataloader(self.train_dataset)

    # Interface
    def get_dev_dataloader(self):
        return self._get_eval_dataloader(self.dev_dataset)

    # Interface
    def get_test_dataloader(self):
        return self._get_eval_dataloader(self.test_dataset)

    # Interface
    def forward(self, mode, features, utter_idx, labels=None, records=None, **kwargs):
        """
        Args:
            features:
                the features extracted by upstream
                put in the device assigned by command-line args

            labels:
                the speaker labels

            records:
                defaultdict(list), by appending scalars into records,
                these scalars will be averaged and logged on Tensorboard

            logger:
                Tensorboard SummaryWriter, given here for logging/debugging
                convenience, please use "self.downstream/your_content_name" as key
                name to log your customized contents

            global_step:
                global_step in runner, which is helpful for Tensorboard logging

        Return:
            loss:
                the loss to be optimized, should not be detached
        """

        features_pad = pad_sequence(features, batch_first=True)
        
        if self.modelrc['module'] == "XVector":
            # TDNN layers in XVector will decrease the total sequence length by fixed 14
            attention_mask = [torch.ones((feature.shape[0] - 14)) for feature in features]
        else:
            attention_mask = [torch.ones((feature.shape[0])) for feature in features]

        attention_mask_pad = pad_sequence(attention_mask,batch_first=True)
        attention_mask_pad = (1.0 - attention_mask_pad) * -100000.0

        features_pad = self.connector(features_pad)

        if mode == 'train':
            agg_vec = self.model(features_pad, attention_mask_pad.cuda())
            labels = torch.LongTensor(labels).to(features_pad.device)
            loss = self.objective(agg_vec, labels)
            records['loss'].append(loss.item())
            return loss
        
        elif mode in ['dev', 'test']:
            agg_vec = self.model.inference(features_pad, attention_mask_pad.cuda())
            agg_vec = torch.nn.functional.normalize(agg_vec,dim=-1)
            utt_name = utter_idx
            
            for idx in range(len(agg_vec)):
                records[utt_name[idx]] = agg_vec[idx].cpu().detach()

            return torch.tensor(0)

    # interface
    def log_records(self, mode, records, logger, global_step, **kwargs):
        """
        Args:
            records:
                defaultdict(list), contents already appended

            logger:
                Tensorboard SummaryWriter
                please use f'{prefix}your_content_name' as key name
                to log your customized contents

            prefix:
                used to indicate downstream and train/test on Tensorboard
                eg. 'phone/train-'

            global_step:
                global_step in runner, which is helpful for Tensorboard logging
        """
        save_names = []

        if mode == 'train':
            loss = torch.FloatTensor(records['loss']).mean().item()
            logger.add_scalar(f'sv-voxceleb1/{mode}-loss', loss, global_step=global_step)
            print(f'sv-voxceleb1/{mode}-loss: {loss}')

        elif mode in ['dev', 'test']:
            trials = self.test_dataset.pair_table
            labels = []
            scores = []
            for label, name1, name2 in trials:
                labels.append(label)
                score = self.score_fn(records[name1], records[name2]).numpy()
                scores.append(score)

            eer, *others = self.eval_metric(np.array(labels, dtype=int), np.array(scores))
            logger.add_scalar(f'sv-voxceleb1/{mode}-EER', eer, global_step=global_step)
            print(f'sv-voxceleb1/{mode}-EER: {eer}')

            if eer < self.best_score and mode == 'dev':
                self.best_score = torch.ones(1) * eer
                save_names.append(f'{mode}-best.ckpt')

            with open(Path(self.expdir) / f"{mode}_predict.txt", "w") as file:
                line = [f"{name} {score}\n" for name, score in zip(records["pair_names"], records["scores"])]
                file.writelines(line)

            with open(Path(self.expdir) / f"{mode}_truth.txt", "w") as file:
                line = [f"{name} {score}\n" for name, score in zip(records["pair_names"], records["labels"])]
                file.writelines(line)
            count = 0
            for label, name1, name2 in trials:
              print(label, name1.split('/')[-1], name2.split('/')[-1], scores[count])
              count += 1
              sleep(0.5)
              if count == 10: break
        return save_names

    def separate_data(self, agg_vec):
        assert len(agg_vec) % 2 == 0
        total_num = len(agg_vec) // 2
        feature1 = agg_vec[:total_num]
        feature2 = agg_vec[total_num:]
        return feature1, feature2
