import numpy as np
import scipy.sparse
import tensorflow as tf

from sklearn.model_selection import train_test_split
from typing import Optional, Dict, Text, List, Tuple, Any, Union, Generator
from collections import defaultdict

from utils import train_utils


class RasaModelData:
    def __init__(self, data: Optional[Dict[Text, List[np.ndarray]]] = None):
        if data is None:
            self.data = {}
        else:
            self.data = data

    def items(self):
        return self.data.items()

    def values(self):
        return self.data.values()

    def keys(self):
        return self.data.keys()

    def split(
        self, number_of_test_examples: int, random_seed: int, label_key: Text
    ) -> Tuple["RasaModelData", "RasaModelData"]:
        """Create random hold out test set using stratified split."""

        self._check_label_key(label_key)

        label_ids = self._create_label_ids(self.data[label_key][0])
        label_counts = dict(zip(*np.unique(label_ids, return_counts=True, axis=0)))

        self._check_train_test_sizes(number_of_test_examples, label_counts)

        counts = np.array([label_counts[label] for label in label_ids])
        multi_values = [v[counts > 1] for values in self.data.values() for v in values]
        solo_values = [v[counts == 1] for values in self.data.values() for v in values]

        output_values = train_test_split(
            *multi_values,
            test_size=number_of_test_examples,
            random_state=random_seed,
            stratify=label_ids[counts > 1],
        )

        return self._convert_train_test_split(output_values, solo_values)

    def add_features(self, key: Text, features: List[np.ndarray]):
        """Add list of features to data under specified key."""

        if not features:
            return

        if key in self.data:
            raise ValueError(f"Key '{key}' already exists in RasaModelData.")

        self.data[key] = []

        for data in features:
            if data.size > 0:
                self.data[key].append(data)

        if not self.data[key]:
            del self.data[key]

    def add_mask(self, key: Text, from_key: Text):
        """Calculate mask for given key and put it under specified key."""

        if not self.data.get(from_key):
            return

        self.data[key] = []

        for data in self.data[from_key]:
            if data.size > 0:
                # explicitly add last dimension to mask
                # to track correctly dynamic sequences
                mask = np.array([np.ones((x.shape[0], 1)) for x in data])
                self.data[key].append(mask)
                break

    def get_signature(self) -> Dict[Text, Tuple[bool, Tuple[int]]]:
        """Get signature of RasaModelData.

        Signature stores the shape and whether features are sparse or not for every
        key."""

        return {
            key: [
                (True if isinstance(v[0], scipy.sparse.spmatrix) else False, v[0].shape)
                for v in values
            ]
            for key, values in self.data.items()
        }

    def shuffle(self) -> None:
        """Shuffle session data."""

        data_points = self.get_number_of_examples()
        ids = np.random.permutation(data_points)
        self.data = self._data_for_ids(ids)

    # noinspection PyPep8Naming
    def balance(self, batch_size: int, shuffle: bool, label_key: Text) -> None:
        """Mix session data to account for class imbalance.

        This batching strategy puts rare classes approximately in every other batch,
        by repeating them. Mimics stratified batching, but also takes into account
        that more populated classes should appear more often.
        """

        if label_key not in self.data or len(self.data[label_key]) > 1:
            raise ValueError(f"Key '{label_key}' not in RasaModelData.")

        label_ids = self._create_label_ids(self.data[label_key][0])

        unique_label_ids, counts_label_ids = np.unique(
            label_ids, return_counts=True, axis=0
        )
        num_label_ids = len(unique_label_ids)

        # need to call every time, so that the data is shuffled inside each class
        label_data = self._split_by_label_ids(label_ids, unique_label_ids)

        data_idx = [0] * num_label_ids
        num_data_cycles = [0] * num_label_ids
        skipped = [False] * num_label_ids

        new_data = defaultdict(list)
        num_examples = self.get_number_of_examples()

        while min(num_data_cycles) == 0:
            if shuffle:
                indices_of_labels = np.random.permutation(num_label_ids)
            else:
                indices_of_labels = range(num_label_ids)

            for index in indices_of_labels:
                if num_data_cycles[index] > 0 and not skipped[index]:
                    skipped[index] = True
                    continue
                else:
                    skipped[index] = False

                index_batch_size = (
                    int(counts_label_ids[index] / num_examples * batch_size) + 1
                )

                for k, values in label_data[index].items():
                    for i, v in enumerate(values):
                        if len(new_data[k]) < i + 1:
                            new_data[k].append([])
                        new_data[k][i].append(
                            v[data_idx[index] : data_idx[index] + index_batch_size]
                        )

                data_idx[index] += index_batch_size
                if data_idx[index] >= counts_label_ids[index]:
                    num_data_cycles[index] += 1
                    data_idx[index] = 0

                if min(num_data_cycles) > 0:
                    break

        final_data = defaultdict(list)
        for k, values in new_data.items():
            for v in values:
                final_data[k].append(np.concatenate(np.array(v)))

        self.data = final_data

    def get_number_of_examples(self) -> int:
        """Obtain number of examples in session data.

        Raise a ValueError if number of examples differ for different data in
        session data.
        """

        example_lengths = [v.shape[0] for values in self.data.values() for v in values]

        # check if number of examples is the same for all values
        if not all(length == example_lengths[0] for length in example_lengths):
            raise ValueError(
                f"Number of examples differs for keys '{self.data.keys()}'. Number of "
                f"examples should be the same for all data."
            )

        return example_lengths[0]

    def get_feature_dimension(self, key: Text) -> int:
        """Get the feature dimension of the given key."""

        number_of_features = 0
        for data in self.data[key]:
            if data.size > 0:
                number_of_features += data[0].shape[-1]

        return number_of_features

    def convert_to_tf_dataset(
        self,
        batch_size: int,
        label_key: Text,
        batch_strategy: Text = "sequence",
        shuffle: bool = False,
    ):
        """Create tf dataset."""

        shapes, types = self._get_shapes_types()

        return tf.data.Dataset.from_generator(
            lambda batch_size_: self._gen_batch(
                batch_size_, label_key, batch_strategy, shuffle
            ),
            output_types=types,
            output_shapes=shapes,
            args=([batch_size]),
        )

    def _get_shapes_types(self) -> Tuple:
        """Extract shapes and types from session data."""

        types = []
        shapes = []

        def append_shape(v: np.ndarray):
            if isinstance(v[0], scipy.sparse.spmatrix):
                # scipy matrix is converted into indices, data, shape
                shapes.append((None, v[0].ndim + 1))
                shapes.append((None,))
                shapes.append((v[0].ndim + 1))
            elif v[0].ndim == 0:
                shapes.append((None,))
            elif v[0].ndim == 1:
                shapes.append((None, v[0].shape[-1]))
            else:
                shapes.append((None, None, v[0].shape[-1]))

        def append_type(v: np.ndarray):
            if isinstance(v[0], scipy.sparse.spmatrix):
                # scipy matrix is converted into indices, data, shape
                types.append(tf.int64)
                types.append(tf.float32)
                types.append(tf.int64)
            else:
                types.append(tf.float32)

        for values in self.data.values():
            for v in values:
                append_shape(v)
                append_type(v)

        return tuple(shapes), tuple(types)

    def _gen_batch(
        self,
        batch_size: int,
        label_key: Text,
        batch_strategy: Text = "sequence",
        shuffle: bool = False,
    ) -> Generator[Tuple, None, None]:
        """Generate batches."""

        if shuffle:
            self.shuffle()

        if batch_strategy == "balanced":
            self.balance(batch_size, shuffle, label_key)

        num_examples = self.get_number_of_examples()
        num_batches = num_examples // batch_size + int(num_examples % batch_size > 0)

        for batch_num in range(num_batches):
            start = batch_num * batch_size
            end = start + batch_size

            yield train_utils.prepare_batch(self.data, start, end)

    def _check_train_test_sizes(
        self, number_of_test_examples: int, label_counts: Dict[Any, int]
    ):
        """Check whether the test data set is too large or too small."""

        number_of_total_examples = self.get_number_of_examples()

        if number_of_test_examples >= number_of_total_examples - len(label_counts):
            raise ValueError(
                f"Test set of {number_of_test_examples} is too large. Remaining "
                f"train set should be at least equal to number of classes "
                f"{len(label_counts)}."
            )
        elif number_of_test_examples < len(label_counts):
            raise ValueError(
                f"Test set of {number_of_test_examples} is too small. It should "
                f"be at least equal to number of classes {label_counts}."
            )

    def _data_for_ids(self, ids: np.ndarray):
        """Filter session data by ids."""

        new_data = defaultdict(list)
        for k, values in self.data.items():
            for v in values:
                new_data[k].append(v[ids])
        return new_data

    def _split_by_label_ids(
        self, label_ids: "np.ndarray", unique_label_ids: "np.ndarray"
    ) -> List["RasaModelData"]:
        """Reorganize session data into a list of session data with the same labels."""

        label_data = []
        for label_id in unique_label_ids:
            ids = label_ids == label_id
            label_data.append(RasaModelData(self._data_for_ids(ids)))
        return label_data

    def _check_label_key(self, label_key: Text):
        if label_key not in self.data or len(self.data[label_key]) > 1:
            raise ValueError(f"Key '{label_key}' not in RasaModelData.")

    def _convert_train_test_split(
        self, output_values: List[Any], solo_values: List[Any]
    ) -> Tuple["RasaModelData", "RasaModelData"]:
        """Convert the output of sklearn.model_selection.train_test_split into train and
        eval session data."""

        data_train = defaultdict(list)
        data_val = defaultdict(list)

        # output_values = x_train, x_val, y_train, y_val, z_train, z_val, etc.
        # order is kept, e.g. same order as session data keys

        # train datasets have an even index
        index = 0
        for key, values in self.data.items():
            for _ in range(len(values)):
                data_train[key].append(
                    self._combine_features(output_values[index * 2], solo_values[index])
                )
                index += 1

        # val datasets have an odd index
        index = 0
        for key, values in self.data.items():
            for _ in range(len(values)):
                data_val[key].append(output_values[(index * 2) + 1])
                index += 1

        return RasaModelData(data_train), RasaModelData(data_val)

    @staticmethod
    def _combine_features(
        feature_1: Union[np.ndarray, scipy.sparse.spmatrix],
        feature_2: Union[np.ndarray, scipy.sparse.spmatrix],
    ) -> Union[np.ndarray, scipy.sparse.spmatrix]:
        """Concatenate features."""

        if isinstance(feature_1, scipy.sparse.spmatrix) and isinstance(
            feature_2, scipy.sparse.spmatrix
        ):
            if feature_2.shape[0] == 0:
                return feature_1
            if feature_1.shape[0] == 0:
                return feature_2
            return scipy.sparse.vstack([feature_1, feature_2])

        return np.concatenate([feature_1, feature_2])

    @staticmethod
    def _create_label_ids(label_ids: np.ndarray) -> np.ndarray:
        """Convert various size label_ids into single dim array.

        For multi-label y, map each distinct row to a string representation
        using join because str(row) uses an ellipsis if len(row) > 1000.
        Idea taken from sklearn's stratify split.
        """

        if label_ids.ndim == 1:
            return label_ids

        if label_ids.ndim == 2 and label_ids.shape[-1] == 1:
            return label_ids[:, 0]

        if label_ids.ndim == 2:
            return np.array([" ".join(row.astype("str")) for row in label_ids])

        if label_ids.ndim == 3 and label_ids.shape[-1] == 1:
            return np.array([" ".join(row.astype("str")) for row in label_ids[:, :, 0]])

        raise ValueError("Unsupported label_ids dimensions")
