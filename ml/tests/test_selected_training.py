import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ml import train


class SelectedTrainingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.prepared = self.root / 'prepared'
        self.prepared.mkdir()
        self.output = self.root / 'leaves_16_min_100'
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'model': 'test', 'num_leaves': 8,
                                         'min_child_samples': 200, 'n_estimators': 500}))
        self.manifest = {'source_sha256': 'source', 'feature_names': ['amount']}
        (self.prepared / 'manifest.json').write_text(json.dumps(self.manifest))
        for name in ('train', 'validation_tuning'):
            (self.prepared / f'{name}.npz').write_bytes(name.encode())
        for name, value in [('PREPARED', self.prepared), ('SELECTED_OUTPUT', self.output), ('CONFIG_PATH', self.config)]:
            mock = patch.object(train, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def complete_run(self):
        self.output.mkdir()
        (self.output / 'model.txt').write_text('saved model')
        meta = {'parameters': {'num_leaves': 16, 'min_child_samples': 100, 'n_estimators': 500},
                'source_sha256': 'source', 'feature_names': ['amount'],
                'prepared_manifest_sha256': train.file_hash(self.prepared / 'manifest.json'),
                'training_arrays_sha256': train.file_hash(self.prepared / 'train.npz'),
                'tuning_arrays_sha256': train.file_hash(self.prepared / 'validation_tuning.npz'),
                'model_sha256': train.file_hash(self.output / 'model.txt')}
        (self.output / 'metadata.json').write_text(json.dumps(meta))
        return meta

    def test_missing_run_trains_only_selected_settings(self):
        with patch.object(train, 'train') as fit:
            train.train_selected()
        fit.assert_called_once_with(output=self.output, overrides={'num_leaves': 16, 'min_child_samples': 100})

    def test_complete_run_is_validated_and_reused(self):
        expected = self.complete_run()
        with patch.object(train, 'train') as fit:
            self.assertEqual(train.train_selected(), expected)
        fit.assert_not_called()

    def test_incomplete_run_is_not_overwritten(self):
        self.output.mkdir()
        marker = self.output / 'model.txt'
        marker.write_text('incomplete')
        with self.assertRaisesRegex(FileExistsError, 'incomplete'):
            train.train_selected()
        self.assertEqual(marker.read_text(), 'incomplete')

    def test_changed_inputs_or_model_are_rejected(self):
        self.complete_run()
        for path in (self.prepared / 'train.npz', self.prepared / 'validation_tuning.npz',
                     self.prepared / 'manifest.json', self.output / 'model.txt'):
            before = path.read_bytes()
            path.write_bytes(before + b' ')
            with self.subTest(path=path), self.assertRaises(ValueError):
                train.train_selected()
            path.write_bytes(before)
