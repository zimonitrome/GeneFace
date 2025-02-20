import torch
import numpy as np
import pickle as pkl
import os, sys
import math, random
from torch.utils.data import Dataset, DataLoader
import tqdm

from utils.commons.hparams import hparams

from tasks.audio2motion.dataset_utils.indexed_db import IndexedDataset
from tasks.audio2motion.dataset_utils.euler2quaterion import euler2quaterion, quaterion2euler

class LRS3SeqDataset(Dataset):
    def __init__(self, prefix='train'):
        self.db_key = prefix
        self.ds_path = hparams['binary_data_dir']
        self.ds = IndexedDataset(os.path.join(self.ds_path, self.db_key))
        self.sizes = None
        self.memory_cache = {} # we use hash table to accelerate indexing

        self.x_multiply = 8

        self.load_db_to_memory()
        self.get_stats()
        self.exp_mean = torch.from_numpy(self.stats_dict['exp_mean']).reshape([1,1,64])
        self.exp_std = torch.from_numpy(self.stats_dict['exp_std']).reshape([1,1,64])
        self.pose_mean = torch.from_numpy(self.stats_dict['pose_mean']).reshape([1,1,7])
        self.pose_std = torch.from_numpy(self.stats_dict['pose_std']).reshape([1,1,7])
        self.exp_std_mean = torch.from_numpy(self.stats_dict['exp_std_mean'])
        self.exp_std_std = torch.from_numpy(self.stats_dict['exp_std_std'])
        self.exp_diff_std_mean = torch.from_numpy(self.stats_dict['exp_diff_std_mean'])
        self.exp_diff_std_std = torch.from_numpy(self.stats_dict['exp_diff_std_std'])
        self.pose_diff_std_mean = torch.from_numpy(self.stats_dict['pose_diff_std_mean'])
        self.pose_diff_std_std = torch.from_numpy(self.stats_dict['pose_diff_std_std'])

        self.idexp_lm3d_mean = torch.from_numpy(self.stats_dict['idexp_lm3d_mean']).reshape([1,68*3])
        self.idexp_lm3d_std = torch.from_numpy(self.stats_dict['idexp_lm3d_std']).reshape([1,68*3])
        self.mouth_idexp_lm3d_mean = torch.from_numpy(self.stats_dict['mouth_idexp_lm3d_mean']).reshape([1,20*3])
        self.mouth_idexp_lm3d_std = torch.from_numpy(self.stats_dict['mouth_idexp_lm3d_std']).reshape([1,20*3])
        self.load_sty_to_memory()

    def __len__(self):
        return len(self.ds)

    def _cal_avatar_style_encoding(self, exp, pose):
        diff_exp = exp[:-1, :] - exp[1:, :]
        exp_std = (torch.std(exp, dim = 0) - self.exp_std_mean) / self.exp_std_std
        diff_exp_std = (torch.std(diff_exp, dim = 0) - self.exp_diff_std_mean) / self.exp_diff_std_std

        diff_pose = pose[:-1, :] - pose[1:, :]
        diff_pose_std = (torch.std(diff_pose, dim = 0) - self.pose_diff_std_mean) / self.pose_diff_std_std

        return torch.cat((exp_std, diff_exp_std, diff_pose_std)) # [135,]

    def ordered_indices(self):
        """Return an ordered list of indices. Batches will be constructed based
        on this order."""
        sizes_fname = os.path.join(self.ds_path, f"sizes_{self.db_key}.npy")
        if os.path.exists(sizes_fname):
            sizes = np.load(sizes_fname, allow_pickle=True)
            self.sizes = sizes
        if self.sizes is None:
            self.sizes = []
            print("Counting the size of each item in dataset...")
            for i_sample in  tqdm.trange(len(self)):
                sample = self.__getitem__(i_sample)
                if sample is None:
                    size = 0
                else:
                    x = sample['mel']
                    size = x.shape[-1] # time step in audio
                self.sizes.append(size)
            np.save(sizes_fname, self.sizes)
        indices = np.arange(len(self))
        indices = indices[np.argsort(np.array(self.sizes)[indices], kind='mergesort')]
        return indices

    def batch_by_size(self, indices, max_tokens=None, max_sentences=None,
        required_batch_size_multiple=1):
        """
        Yield mini-batches of indices bucketed by size. Batches may contain
        sequences of different lengths.

        Args:
            indices (List[int]): ordered list of dataset indices
            num_tokens_fn (callable): function that returns the number of tokens at
                a given index
            max_tokens (int, optional): max number of tokens in each batch
                (default: None).
            max_sentences (int, optional): max number of sentences in each
                batch (default: None).
            required_batch_size_multiple (int, optional): require batch size to
                be a multiple of N (default: 1).
        """
        def _is_batch_full(batch, num_tokens, max_tokens, max_sentences):
            if len(batch) == 0:
                return 0
            if len(batch) == max_sentences:
                return 1
            if num_tokens > max_tokens:
                return 1
            return 0

        num_tokens_fn = lambda x: self.sizes[x]
        max_tokens = max_tokens if max_tokens is not None else 60000
        max_sentences = max_sentences if max_sentences is not None else 512
        bsz_mult = required_batch_size_multiple

        sample_len = 0
        sample_lens = []
        batch = []
        batches = []
        for i in range(len(indices)):
            idx = indices[i]
            num_tokens = num_tokens_fn(idx)
            sample_lens.append(num_tokens)
            sample_len = max(sample_len, num_tokens)

            assert sample_len <= max_tokens, (
                "sentence at index {} of size {} exceeds max_tokens "
                "limit of {}!".format(idx, sample_len, max_tokens)
            )
            num_tokens = (len(batch) + 1) * sample_len

            if _is_batch_full(batch, num_tokens, max_tokens, max_sentences):
                mod_len = max(
                    bsz_mult * (len(batch) // bsz_mult),
                    len(batch) % bsz_mult,
                )
                batches.append(batch[:mod_len])
                batch = batch[mod_len:]
                sample_lens = sample_lens[mod_len:]
                sample_len = max(sample_lens) if len(sample_lens) > 0 else 0
            batch.append(idx)
        if len(batch) > 0:
            batches.append(batch)
        return batches

    def decode_pose(self, pose):
        """
        pose [B, T, C=7=4+3]
        """
        b,t,_ = pose.shape
        if self.normalize_target:
            pose = pose * self.pose_std + self.pose_mean
        translations = pose[:, :, :3].cpu().numpy() # [B, T, 3]
        angles = pose[:, :, 3:].cpu().numpy() # [B, T, 4]
        angles = quaterion2euler(angles.reshape([b*t,4])) # [B*T, 3]
        angles = angles.reshape([b,t,3])
        return angles, translations

    def load_db_to_memory(self):
        for idx in tqdm.trange(len(self), desc='Loading database to memory...'):
            raw_item = self.ds[idx]
            if raw_item is None:
                print("loading from binary data failed!")
                continue
            item = {}
            item_id = raw_item['item_id'] # str: "<speakerID>_<clipID>"
            item['item_id'] = item_id
            # audio-related features
            mel = raw_item['mel']
            hubert = raw_item['hubert']
            # energy = raw_item['energy']
            item['mel'] = torch.from_numpy(mel).float() # [T_x, c=80]
            item['hubert'] = torch.from_numpy(hubert).float() # [T_x, c=80]
            # item['energy'] = torch.from_numpy(energy).float() # [T_x, c=1]
            # video-related features
            coeff = raw_item['coeff'] # [T_y ~= T_x//2, c=257]
            exp = coeff[:, 80:144] 
            item['exp'] = torch.from_numpy(exp).float() # [T_y, c=64]
            translation = coeff[:, 254:257] # [T_y, c=3]
            angles = euler2quaterion(coeff[:, 224:227]) # # [T_y, c=4]
            pose = np.concatenate([translation, angles], axis=1)
            item['pose'] = torch.from_numpy(pose).float() # [T_y, c=4+3]

            # Load eye blinks and brow raises from OpenFace
            # item['au02_r'] = torch.from_numpy(raw_item['au02_r']).float().unsqueeze(-1) # [T_x, c=1]
            item['au45_r'] = torch.from_numpy(raw_item['au45_r']).float().unsqueeze(-1) # [T_x, c=1]

            # Load identity for landmark construction
            item['identity'] = torch.from_numpy(raw_item['coeff'][..., :80]).float()
            
            # Load lm3d
            t_lm, dim_lm, _ = raw_item['lm3d'].shape # [T, 68, 3]
            item['lm3d'] = torch.from_numpy(raw_item['lm3d']).reshape(t_lm, -1).float()
            item['mouth_lm3d'] = torch.from_numpy(raw_item['mouth_lm3d']).reshape(t_lm, -1).float()
            item['idexp_lm3d'] = torch.from_numpy(raw_item['idexp_lm3d']).reshape(t_lm, -1).float()
            item['eye_idexp_lm3d'] = torch.from_numpy(raw_item['eye_idexp_lm3d']).reshape(t_lm, -1).float()
            item['mouth_idexp_lm3d'] = torch.from_numpy(raw_item['mouth_idexp_lm3d']).reshape(t_lm, -1).float()
            
            self.memory_cache[idx] = item

    def __getitem__(self, idx):
        item = self.memory_cache[idx]
        item['ref_mean_lm3d'] = item['idexp_lm3d'].mean(dim=0).reshape([204,])
        return item
        
    def get_stats(self):
        stats_fname = os.path.join(self.ds_path, "stats.npy")
        if os.path.exists(stats_fname):
            self.stats_dict = np.load(stats_fname,allow_pickle=True).tolist()
            print(f"load from cached stats.npy from {stats_fname}.")
            return
        if self.db_key != 'train':
            raise ValueError("Please use train-dataset to generate the stats file!")
        print(f"stats.npy not found, generating...")
        exp_lst = []
        pose_lst = []
        idexp_lm3d_lst = []
        mouth_idexp_lm3d_lst = []
        for i in range(len(self)):
            item = self.__getitem__(i)
            exp_lst.append(item['exp'].numpy())
            pose_lst.append(item['pose'].numpy())
            idexp_lm3d_lst.append(item['idexp_lm3d'].numpy())
            mouth_idexp_lm3d_lst.append(item['mouth_idexp_lm3d'].numpy())
        exps = np.concatenate(exp_lst)
        poses = np.concatenate(pose_lst)
        exp_mean = exps.mean(axis=0)
        exp_std = exps.std(axis=0)
        pose_mean = poses.mean(axis=0)
        pose_std = poses.std(axis=0)

        idexp_lm3ds = np.concatenate(idexp_lm3d_lst)
        mouth_idexp_lm3ds = np.concatenate(mouth_idexp_lm3d_lst)
        idexp_lm3d_mean = idexp_lm3ds.mean(axis=0)
        idexp_lm3d_std = idexp_lm3ds.std(axis=0)
        mouth_idexp_lm3d_mean = mouth_idexp_lm3ds.mean(axis=0)
        mouth_idexp_lm3d_std = mouth_idexp_lm3ds.std(axis=0)

        exp_std_mean = np.stack([np.std(exp, axis=0) for exp in exp_lst],axis=0).mean(axis=0)
        exp_std_std = np.stack([np.std(exp, axis=0) for exp in exp_lst],axis=0).std(axis=0)

        diff_exp_lst = [exp[:-1,:]-exp[1:,:] for exp in exp_lst]
        exp_diff_std_mean = np.stack([np.std(diff_exp, axis=0) for diff_exp in diff_exp_lst],axis=0).mean(axis=0)
        exp_diff_std_std = np.stack([np.std(diff_exp, axis=0) for diff_exp in diff_exp_lst],axis=0).std(axis=0)

        diff_pose_lst = [pose[:-1, :] - pose[1:, :] for pose in pose_lst]
        pose_diff_std_mean = np.stack([np.std(diff_pose, axis=0) for diff_pose in diff_pose_lst],axis=0).mean(axis=0)
        pose_diff_std_std = np.stack([np.std(diff_pose, axis=0) for diff_pose in diff_pose_lst],axis=0).std(axis=0)

        stats_dict = {
            "exp_mean": exp_mean,
            "exp_std": exp_std,
            "pose_mean": pose_mean,
            "pose_std": pose_std,

            # 3d lm related
            "idexp_lm3d_mean": idexp_lm3d_mean,
            "idexp_lm3d_std": idexp_lm3d_std,
            "mouth_idexp_lm3d_mean": mouth_idexp_lm3d_mean,
            "mouth_idexp_lm3d_std": mouth_idexp_lm3d_std,

            # style encoding related
            "exp_std_mean": exp_std_mean,
            "exp_std_std": exp_std_std,
            "exp_diff_std_mean": exp_diff_std_mean,
            "exp_diff_std_std": exp_diff_std_std,
            "pose_diff_std_mean": pose_diff_std_mean,
            "pose_diff_std_std": pose_diff_std_std,
        }
        np.save(stats_fname, stats_dict)
        self.stats_dict = stats_dict
        print(f"stats.npy generated into {stats_fname}.")


    def load_sty_to_memory(self):
        for i in tqdm.trange(len(self), desc="calculating style_feat"):
            item = self.memory_cache[i]
            # calculate style encoding from style-avatar
            sty = self._cal_avatar_style_encoding(item['exp'], item['pose'])
            item['style'] = sty.float()

            # item['idexp_lm3d_normalized'] = (item['idexp_lm3d'] - self.idexp_lm3d_mean) / self.idexp_lm3d_std
            # item['mouth_idexp_lm3d_normalized'] = (item['mouth_idexp_lm3d'] - self.mouth_idexp_lm3d_mean) / self.mouth_idexp_lm3d_std


    @staticmethod
    def _collate_2d(values, max_len=None, pad_value=0):
        """
        Convert a list of 2d tensors into a padded 3d tensor.
            values: list of Batch tensors with shape [T, C]
            return: [B, T, C]
        """
        max_len = max(v.size(0) for v in values) if max_len is None else max_len
        hidden_dim = values[0].size(1)
        batch_size = len(values)
        ret = torch.ones([batch_size,  max_len, hidden_dim],dtype=values[0].dtype) * pad_value
        for i, v in enumerate(values):
            ret[i, :v.shape[0], :].copy_(v)
        return ret

    def collater(self, samples):
        none_idx = []
        for i in range(len(samples)):
            if samples[i] is None:
                none_idx.append(i)
        for i in sorted(none_idx, reverse=True):
            del samples[i]
        if len(samples) == 0:
            return None
        batch = {}
        item_names = [s['item_id'] for s in samples]
        style_batch = torch.stack([s["style"] for s in samples], dim=0) # [b, 135]
        x_len = max(s['mel'].size(0) for s in samples)
        x_len = x_len + (self.x_multiply - (x_len % self.x_multiply)) % self.x_multiply
        y_len = x_len // 2
        mel_batch = self._collate_2d([s["mel"] for s in samples], max_len=x_len, pad_value=0) # [b, t_max_y, 64]
        hubert_batch = self._collate_2d([s["hubert"] for s in samples], max_len=x_len, pad_value=0) # [b, t_max_y, 64]
        # audio_batch = self._collate_2d([s["audio"] for s in samples], max_len=x_len, pad_value=0) # [b, t_max_y, 29]
        # energy_batch = self._collate_2d([s["energy"] for s in samples], max_len=x_len, pad_value=0) # [b, t_max_y, 1]
        exp_batch = self._collate_2d([s["exp"] for s in samples], max_len=y_len, pad_value=0) # [b, t_max_y, 64]
        pose_batch = self._collate_2d([s["pose"] for s in samples], max_len=y_len, pad_value=0) # [b, t_max_y, 64]
        
        # lm3d = self._collate_2d([s["lm3d"] for s in samples], max_len=y_len, pad_value=0) # [b, t_max, 1]
        # mouth_lm3d = self._collate_2d([s["mouth_lm3d"] for s in samples], max_len=y_len, pad_value=0) # [b, t_max, 1]
        idexp_lm3d = self._collate_2d([s["idexp_lm3d"] for s in samples], max_len=y_len, pad_value=0) # [b, t_max, 1]
        ref_mean_lm3d = torch.stack([s['ref_mean_lm3d'] for s in samples], dim=0) # [b, h=204*5]
        # idexp_lm3d_normalized = self._collate_2d([s["idexp_lm3d_normalized"] for s in samples], max_len=y_len, pad_value=0) # [b, t_max, 1]
        # eye_idexp_lm3d = self._collate_2d([s["eye_idexp_lm3d"] for s in samples], max_len=y_len, pad_value=0) # [b, t_max, 1]
        mouth_idexp_lm3d = self._collate_2d([s["mouth_idexp_lm3d"] for s in samples], max_len=y_len, pad_value=0) # [b, t_max, 1]
        # mouth_idexp_lm3d_normalized = self._collate_2d([s["mouth_idexp_lm3d_normalized"] for s in samples], max_len=y_len, pad_value=0) # [b, t_max, 1]

        # au02_r = self._collate_2d([s["au02_r"] for s in samples], max_len=y_len, pad_value=0) # [b, t_max, 1]
        au45_r = self._collate_2d([s["au45_r"] for s in samples], max_len=y_len, pad_value=0) # [b, t_max, 1]

        x_mask = (mel_batch.abs().sum(dim=-1) > 0).float() # [b, t_max_x]
        y_mask = (pose_batch.abs().sum(dim=-1) > 0).float() # [b, t_max_y]

        batch.update({
            'item_id': item_names,
            'style': style_batch,
            'mel': mel_batch,
            'hubert': hubert_batch,
            # 'audio': audio_batch,
            # 'energy': energy_batch,
            'x_mask': x_mask,
            'exp': exp_batch,
            'pose': pose_batch,
            'y_mask': y_mask,
            'idexp_lm3d': idexp_lm3d,
            'ref_mean_lm3d': ref_mean_lm3d,
            # 'idexp_lm3d_normalized': idexp_lm3d_normalized,
            'mouth_idexp_lm3d': mouth_idexp_lm3d,
            # 'mouth_idexp_lm3d_normalized': mouth_idexp_lm3d_normalized,
            # 'au02_r': au02_r,
            'au45_r': au45_r,
        })
        return batch

    def get_dataloader(self):
        shuffle = True if self.db_key == 'train' else False
        max_tokens = 60000
        batches_idx = self.batch_by_size(self.ordered_indices(), max_tokens=max_tokens)
        loader = DataLoader(self, pin_memory=False,collate_fn=self.collater, batch_sampler=batches_idx, num_workers=4)
        return loader


if __name__ == '__main__':
    ds = LRS3SeqDataset('train')
    loader = ds.get_dataloader()
    pbar = tqdm.tqdm(total=len(ds.batch_by_size(ds.ordered_indices())))
    for batch in loader:
        pbar.update(1)