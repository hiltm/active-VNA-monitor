# plugins/ml_engine.py
import logging
from pathlib import Path
import numpy as np
import joblib
from core.models import MeasurementRecord

logger = logging.getLogger("VNASuite")

CLASS_LABELS = {
    0: 'Fresh (Baseline)',
    1: 'Lightly Aged (Days 1-3)',
    2: 'Heavily Aged (Days 4+)',
}

N_FREQ_POINTS = 201


class RFAgingClassifier:
    """RF Reflectometry Board Aging Classifier for 3-class tamper/aging inference."""

    def __init__(self, model_dir: str, model_type: str = 'rf'):
        self.model_dir = Path(model_dir)
        self.model_type = model_type
        self.model = None
        self.scaler = None
        self.metadata = None

    def load_model(self):
        """Loads the trained classifier, StandardScaler, and training metadata."""
        model_path = self.model_dir / f"{self.model_type}_model.joblib"
        scaler_path = self.model_dir / "scaler.joblib"
        metadata_path = self.model_dir / "training_metadata.joblib"

        if not model_path.exists():
            raise FileNotFoundError(f"Model file not found: {model_path}")
        if not scaler_path.exists():
            raise FileNotFoundError(f"Scaler file not found: {scaler_path}")

        self.model = joblib.load(model_path)
        self.scaler = joblib.load(scaler_path)

        if metadata_path.exists():
            self.metadata = joblib.load(metadata_path)

        logger.info(f"Loaded {self.model_type.upper()} model and StandardScaler from {self.model_dir}")

    @staticmethod
    def extract_features(s11_complex: np.ndarray, s21_complex: np.ndarray, freqs: np.ndarray = None) -> np.ndarray:
        """
        Extracts the full 1,612-element feature vector from complex S11 and S21 arrays.
        Matches train_classifier.py and classify_measurement.py.
        """
        if freqs is None:
            freqs = np.linspace(1e6, 4e9, N_FREQ_POINTS)

        # Magnitude in dB
        s11_mag_db = 20 * np.log10(np.maximum(np.abs(s11_complex), 1e-10))
        s21_mag_db = 20 * np.log10(np.maximum(np.abs(s21_complex), 1e-10))

        # Phase in degrees
        s11_phase = np.angle(s11_complex, deg=True)
        s21_phase = np.angle(s21_complex, deg=True)

        # Group delay: -d(unwrapped_phase)/d(freq) in nanoseconds
        s11_phase_unwrap = np.unwrap(np.angle(s11_complex))
        s21_phase_unwrap = np.unwrap(np.angle(s21_complex))
        freq_diff = np.diff(freqs)
        s11_group_delay_ns = -np.diff(s11_phase_unwrap) / (2 * np.pi * freq_diff) * 1e9
        s21_group_delay_ns = -np.diff(s21_phase_unwrap) / (2 * np.pi * freq_diff) * 1e9

        # Magnitude slope: d(mag_dB)/d(freq_GHz)
        freq_diff_ghz = freq_diff / 1e9
        s11_mag_slope = np.diff(s11_mag_db) / freq_diff_ghz
        s21_mag_slope = np.diff(s21_mag_db) / freq_diff_ghz

        # Statistical summaries
        stats = np.array([
            np.mean(s11_mag_db), np.std(s11_mag_db),
            np.mean(s21_mag_db), np.std(s21_mag_db),
            np.mean(s11_phase), np.std(s11_phase),
            np.mean(s21_phase), np.std(s21_phase),
        ])

        features = np.concatenate([
            s11_mag_db, s11_phase, s21_mag_db, s21_phase,
            s11_group_delay_ns, s21_group_delay_ns,
            s11_mag_slope, s21_mag_slope,
            stats,
        ])
        return features

    def evaluate(self, record: MeasurementRecord) -> dict:
        """
        Runs real-time inference on a MeasurementRecord.
        Returns class id, label, confidence score, and probability distribution.
        """
        if self.model is None or self.scaler is None:
            return {
                'class': None,
                'label': 'Model Not Loaded',
                'confidence': 0.0,
                'probabilities': [0.0, 0.0, 0.0]
            }

        # Extract 1612 features and apply standard scaling
        raw_features = self.extract_features(record.s11, record.s21, record.freqs)
        features_scaled = self.scaler.transform(raw_features.reshape(1, -1))

        # Predict class
        predicted_class = int(self.model.predict(features_scaled)[0])

        probabilities = [0.0, 0.0, 0.0]
        confidence = 0.0
        if hasattr(self.model, 'predict_proba'):
            probs = self.model.predict_proba(features_scaled)[0]
            probabilities = [float(p) for p in probs]
            confidence = float(probabilities[predicted_class])

        return {
            'class': predicted_class,
            'label': CLASS_LABELS.get(predicted_class, f"Class {predicted_class}"),
            'confidence': confidence,
            'probabilities': probabilities,
        }