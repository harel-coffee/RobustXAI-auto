import os
import logging
import torch
import pandas as pd
import numpy as np
import networkx as nx
import random
import h5py
from pathlib import Path
from torch.utils.data import Dataset, SubsetRandomSampler
from imblearn.over_sampling import SMOTE
from abc import ABC, abstractmethod
from torch_geometric.datasets import TUDataset
from utils.misc import to_molecule
from joblib import Parallel, delayed
from tqdm import tqdm


class ConceptDataset(ABC, Dataset):

    @property
    @abstractmethod
    def concept_names(self):
        ...

    @abstractmethod
    def generate_concept_dataset(self, concept_id: int, concept_set_size: int) -> tuple:
        ...


class ECGDataset(ConceptDataset):

    def __init__(self, data_dir: Path, train: bool, balance_dataset: bool,
                 random_seed: int = 42, binarize_label: bool = True):
        """
        Generate a ECG dataset
        Args:
            data_dir: directory where the dataset should be stored
            train: True if the training set should be returned, False for the testing set
            balance_dataset: True if the classes should be balanced with SMOTE
            random_seed: random seed for reproducibility
            binarize_label: True if the label should be binarized (0: normal heartbeat, 1: abnormal heartbeat)
        """
        self.data_dir = data_dir
        if not data_dir.exists():
            os.makedirs(data_dir)
            self.download()
        # Read CSV; extract features and labels
        file_path = data_dir / "mitbih_train.csv" if train else data_dir / "mitbih_test.csv"
        df = pd.read_csv(file_path)
        X = df.iloc[:, :187].values
        y = df.iloc[:, 187].values
        if balance_dataset:
            n_normal = np.count_nonzero(y == 0)
            balancing_dic = {0: n_normal, 1: int(n_normal / 4), 2: int(n_normal / 4),
                             3: int(n_normal / 4), 4: int(n_normal / 4)}
            smote = SMOTE(random_state=random_seed, sampling_strategy=balancing_dic)
            X, y = smote.fit_resample(X, y)
        if binarize_label:
            y = np.where(y >= 1, 1, 0)
        self.X = torch.tensor(X, dtype=torch.float32).unsqueeze(1)
        self.y = torch.tensor(y, dtype=torch.long)
        self.binarize_label = binarize_label

    def __len__(self):
        return len(self.y)

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]

    def download(self) -> None:
        import kaggle
        logging.info(f"Downloading ECG dataset in {self.data_dir}")
        kaggle.api.authenticate()
        kaggle.api.dataset_download_files('shayanfazeli/heartbeat', path=self.data_dir, unzip=True)
        logging.info(f"ECG dataset downloaded in {self.data_dir}")

    def generate_concept_dataset(self, concept_id: int, concept_set_size: int) -> tuple:
        """
        Return a concept dataset with positive/negatives for ECG
        Args:
            random_seed: random seed for reproducibility
            concept_set_size: size of the positive and negative subset
        Returns:
            a concept dataset of the form X (features),C (concept labels)
        """
        assert not self.binarize_label
        mask = self.y == concept_id + 1
        positive_idx = torch.nonzero(mask).flatten()
        negative_idx = torch.nonzero(~mask).flatten()
        positive_loader = torch.utils.data.DataLoader(self, batch_size=concept_set_size, sampler=SubsetRandomSampler(positive_idx))
        negative_loader = torch.utils.data.DataLoader(self, batch_size=concept_set_size, sampler=SubsetRandomSampler(negative_idx))
        X_pos, C_pos = next(iter(positive_loader))
        X_neg, C_neg = next(iter(negative_loader))
        X = torch.concatenate((X_pos, X_neg), 0)
        C = torch.concatenate((torch.ones(concept_set_size), torch.zeros(concept_set_size)), 0)
        rand_perm = torch.randperm(len(X))
        return X[rand_perm], C[rand_perm]

    def concept_names(self):
        return ["Supraventricular", "Premature Ventricular", "Fusion Beats", "Unknown"]


class MutagenicityDataset(ConceptDataset, Dataset):

    def __init__(self, data_dir: Path, train: bool, random_seed: int = 11):
        torch.manual_seed(random_seed)
        dataset = TUDataset(str(data_dir), name='Mutagenicity').shuffle()
        self.dataset = dataset[len(dataset) // 10:] if train else dataset[:len(dataset) // 10]
        self.random_seed = random_seed

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, idx):
        return self.dataset.get(idx)

    def generate_concept_dataset(self, concept_id: int, concept_set_size: int) -> tuple:
        concept_detectors = {'Nitroso': self.is_nitroso, 'Aliphatic Halide': self.is_aliphatic_halide,
                             'Azo Type': self.is_azo_type, 'Nitro Type': self.is_nitro_type}
        concept_detector = concept_detectors[self.concept_names()[concept_id]]
        mask = []
        for graph in iter(self.dataset):
            molecule = to_molecule(graph)
            mask.append(concept_detector(molecule))
        mask = torch.tensor(mask)
        positive_set = self.dataset[mask][:concept_set_size]
        negative_set = self.dataset[~mask][:concept_set_size]
        concept_set = positive_set+negative_set
        C = torch.concatenate((torch.ones(len(positive_set)), torch.zeros(len(negative_set))), 0)
        return concept_set, C

    def concept_names(self):
        return ['Nitroso', 'Aliphatic Halide', 'Azo Type', 'Nitro Type']

    @staticmethod
    def is_nitroso(molecule: nx.Graph) -> bool:
        atoms = nx.get_node_attributes(molecule, 'name')
        valences = nx.get_edge_attributes(molecule, 'valence')
        for node1 in molecule.nodes:
            if atoms[node1] == 'N':
                for node2 in molecule.adj[node1]:
                    if atoms[node2] == 'O' and valences[node1, node2] == 2:
                        return True
        return False

    @staticmethod
    def is_aliphatic_halide(molecule: nx.Graph) -> bool:
        atoms = nx.get_node_attributes(molecule, 'name')
        for node1 in molecule.nodes:
            if atoms[node1] in {'Cl', 'Br', 'I'}:
                return True
        return False

    @staticmethod
    def is_azo_type(molecule: nx.Graph) -> bool:
        atoms = nx.get_node_attributes(molecule, 'name')
        valences = nx.get_edge_attributes(molecule, 'valence')
        for node1 in molecule.nodes:
            if atoms[node1] == 'N':
                for node2 in nx.neighbors(molecule, node1):
                    if atoms[node2] == 'N' and valences[node1, node2] == 2:
                        return True
        return False

    @staticmethod
    def is_nitro_type(molecule: nx.Graph) -> bool:
        atoms = nx.get_node_attributes(molecule, 'name')
        valences = nx.get_edge_attributes(molecule, 'valence')
        for node1 in molecule.nodes:
            if atoms[node1] == 'N':
                has_single_NO = False
                has_double_NO = False
                for node2 in nx.neighbors(molecule, node1):
                    if atoms[node2] == 'O':
                        match valences[node1, node2]:
                            case 1:
                                has_single_NO = True
                            case 2:
                                has_double_NO = True

                if has_single_NO and has_double_NO:
                    return True
        return False


class ModelNet40Dataset(ConceptDataset):

    def __init__(self, data_dir: Path, train: bool, random_seed: int = 42):
        """
        Generate a ModelNet40 dataset
        Args:
            data_dir: directory where the dataset should be stored
            train: True if the training set should be returned, False for the testing set
            random_seed: random seed for reproducibility
        """
        self.data_dir = data_dir
        self.random_seed = random_seed
        if not data_dir.exists():
            os.makedirs(data_dir)
            self.download()
        if not (self.data_dir/'ModelNet_40_npy').exists():
            self.preprocess()
        if not (self.data_dir/'ModelNet40_cloud.h5').exists():
            self.formatting()

    def __len__(self):
        ...

    def __getitem__(self, idx):
        ...

    def download(self) -> None:
        import kaggle
        logging.info(f"Downloading ModelNet40 dataset in {self.data_dir}")
        kaggle.api.authenticate()
        kaggle.api.dataset_download_files('balraj98/modelnet40-princeton-3d-object-dataset', path=self.data_dir, unzip=True)
        logging.info(f"ECG dataset downloaded in {self.data_dir}")

    def preprocess(self) -> None:
        """
        Preprocessing code adapted from https://github.com/michaelsdr/sinkformers/blob/main/model_net_40/to_h5.py
        :return:
        """
        random.seed(self.random_seed)
        path = self.data_dir/"ModelNet40"
        folders = [dir for dir in sorted(os.listdir(path)) if os.path.isdir(path / dir)]
        classes = {folder: i for i, folder in enumerate(folders)}

        def read_off(file):
            header = file.readline().strip()
            if 'OFF' not in header:
                raise ValueError('Not a valid OFF header')
            if header != 'OFF':  # The header is merged with the first line
                second_line = header.replace('OFF', '')
            else:  # The second line can be read directly
                 second_line = file.readline()
            n_verts, n_faces, __ = tuple([int(s) for s in second_line.strip().split(' ')])
            verts = [[float(s) for s in file.readline().strip().split(' ')] for i_vert in range(n_verts)]
            faces = [[int(s) for s in file.readline().strip().split(' ')][1:] for i_face in range(n_faces)]
            return verts, faces

        with open(path / "bed/train/bed_0001.off", 'r') as f:
            verts, faces = read_off(f)

        i, j, k = np.array(faces).T
        x, y, z = np.array(verts).T

        class PointSampler(object):
            def __init__(self, output_size):
                assert isinstance(output_size, int)
                self.output_size = output_size

            def triangle_area(self, pt1, pt2, pt3):
                side_a = np.linalg.norm(pt1 - pt2)
                side_b = np.linalg.norm(pt2 - pt3)
                side_c = np.linalg.norm(pt3 - pt1)
                s = 0.5 * (side_a + side_b + side_c)
                return max(s * (s - side_a) * (s - side_b) * (s - side_c), 0) ** 0.5

            def sample_point(self, pt1, pt2, pt3):
                # barycentric coordinates on a triangle
                # https://mathworld.wolfram.com/BarycentricCoordinates.html
                s, t = sorted([random.random(), random.random()])
                f = lambda i: s * pt1[i] + (t - s) * pt2[i] + (1 - t) * pt3[i]
                return (f(0), f(1), f(2))

            def __call__(self, mesh):
                verts, faces = mesh
                verts = np.array(verts)
                areas = np.zeros((len(faces)))

                for i in range(len(areas)):
                    areas[i] = (self.triangle_area(verts[faces[i][0]],
                                                   verts[faces[i][1]],
                                                   verts[faces[i][2]]))

                sampled_faces = (random.choices(faces,
                                                weights=areas,
                                                cum_weights=None,
                                                k=self.output_size))

                sampled_points = np.zeros((self.output_size, 3))

                for i in range(len(sampled_faces)):
                    sampled_points[i] = (self.sample_point(verts[sampled_faces[i][0]],
                                                           verts[sampled_faces[i][1]],
                                                           verts[sampled_faces[i][2]]))

                return sampled_points

        pointcloud = PointSampler(10000)((verts, faces))

        def process(file, file_adr, save_adr):
            fname = save_adr/f'{file[:-4]}.npy'
            if file_adr.suffix == '.off':
                if not os.path.isfile(fname):
                    with open(file_adr, 'r') as f:
                        verts, faces = read_off(f)
                        pointcloud = PointSampler(10000)((verts, faces))
                        np.save(fname, pointcloud)
                else:
                    pass

        tr_label = []
        tr_cloud = []
        test_cloud = []
        test_label = []

        folder = 'train'
        root_dir = path
        folders = [dir for dir in sorted(os.listdir(root_dir)) if os.path.isdir(root_dir / dir)]
        classes = {folder: i for i, folder in enumerate(folders)}
        files = []

        all_files = []
        all_files_adr = []
        all_save_adr = []

        for category in classes.keys():
            save_adr = self.data_dir/f'ModelNet_40_npy/{category}/{folder}'
            try:
                os.makedirs(save_adr)
            except:
                pass
            new_dir = root_dir / Path(category) / folder
            for file in os.listdir(new_dir):
                all_files.append(file)
                all_files_adr.append(new_dir / file)
                all_save_adr.append(save_adr)

        logging.info('Now processing the training files')
        Parallel(n_jobs=40)(delayed(process)(file, file_adr, save_adr) for (file, file_adr, save_adr)
                            in tqdm(zip(all_files, all_files_adr, all_save_adr), leave=False, unit='files'))

        folder = 'test'
        root_dir = path
        folders = [dir for dir in sorted(os.listdir(root_dir)) if os.path.isdir(root_dir / dir)]
        classes = {folder: i for i, folder in enumerate(folders)}
        files = []

        all_files = []
        all_files_adr = []
        all_save_adr = []

        for category in classes.keys():
            save_adr = self.data_dir/f'ModelNet_40_npy/{category}/{folder}'
            try:
                os.makedirs(save_adr)
            except:
                pass
            new_dir = root_dir / Path(category) / folder
            for file in os.listdir(new_dir):
                all_files.append(file)
                all_files_adr.append(new_dir / file)
                all_save_adr.append(save_adr)

        logging.info('Now processing the test files')
        Parallel(n_jobs=40)(delayed(process)(file, file_adr, save_adr) for (file, file_adr, save_adr)
                            in tqdm(zip(all_files, all_files_adr, all_save_adr), leave=False, unit='files'))

    def formatting(self) -> None:
        """
            Preprocessing code adapted from https://github.com/michaelsdr/sinkformers/blob/main/model_net_40/formatting.py
            :return:
        """
        path = self.data_dir/"ModelNet_40_npy"

        folders = [dir for dir in sorted(os.listdir(path)) if os.path.isdir(path / dir)]
        classes = {folder: i for i, folder in enumerate(folders)}

        tr_label = []
        tr_cloud = []
        test_cloud = []
        test_label = []

        logging.info('Now formatting training files')
        folder = 'train'
        root_dir = path
        folders = [dir for dir in sorted(os.listdir(root_dir)) if os.path.isdir(root_dir / dir)]
        classes = {folder: i for i, folder in enumerate(folders)}
        files = []

        all_files = []
        all_files_adr = []
        all_save_adr = []

        for (category, num) in zip(classes.keys(), classes.values()):
            new_dir = root_dir / Path(category) / folder

            for file in os.listdir(new_dir):
                if file.endswith('.npy'):
                    try:
                        point_cloud = np.load(new_dir / file)
                        tr_cloud.append(point_cloud)
                        tr_label.append(num)
                    except:
                        pass
        tr_cloud = np.asarray(tr_cloud)
        tr_label = np.asarray(tr_label)
        np.save(str(self.data_dir/'tr_cloud.npy'), tr_cloud)
        np.save(str(self.data_dir/'tr_label.npy'), tr_label)

        logging.info('Now formatting test files')
        folder = 'test'
        root_dir = path
        folders = [dir for dir in sorted(os.listdir(root_dir)) if os.path.isdir(root_dir / dir)]
        classes = {folder: i for i, folder in enumerate(folders)}
        files = []

        for (category, num) in zip(classes.keys(), classes.values()):
            new_dir = root_dir / Path(category) / folder

            for file in os.listdir(new_dir):
                if file.endswith('.npy'):
                    try:
                        point_cloud = np.load(new_dir / file)
                        test_cloud.append(point_cloud)
                        test_label.append(num)
                    except:
                        pass

        test_cloud = np.asarray(test_cloud)
        test_label = np.asarray(test_label)
        np.save(str(self.data_dir/'test_cloud.npy'), test_cloud)
        np.save(str(self.data_dir/'test_label.npy'), test_label)

        with h5py.File(self.data_dir/'ModelNet40_cloud.h5', 'w') as f:
            f.create_dataset("test_cloud", data=test_cloud)
            f.create_dataset("tr_cloud", data=tr_cloud)
            f.create_dataset("test_label", data=test_label)
            f.create_dataset("tr_label", data=tr_label)

    def generate_concept_dataset(self, concept_id: int, concept_set_size: int) -> tuple:
        """
        Return a concept dataset with positive/negatives for ECG
        Args:
            random_seed: random seed for reproducibility
            concept_set_size: size of the positive and negative subset
        Returns:
            a concept dataset of the form X (features),C (concept labels)
        """
        ...

    def concept_names(self):
        ...