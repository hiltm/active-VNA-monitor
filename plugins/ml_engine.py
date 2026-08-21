# plugins/ml_engine.py
import logging
from pathlib import Path
import numpy as np
from core.models import MeasurementRecord

logger = logging.getLogger("VNASuite")

class MLEngine:
    """Loads a pre-trained ML model and runs inference on incoming VNA sweeps."""

    def __init__(self, model_path: str, threshold: float = 0.85, feature_type: str = "magnitude_db"):
        self.model_path = Path(model_path)
        self.threshold = threshold
        self.feature_type = feature_type
        self.model = None
        self.model_type = None

    def load_model(self):
        """Loads ONNX, TFLite, Joblib (scikit-learn), or PyTorch models."""
        if not self.model_path.exists():
            raise FileNotFoundError(f"ML Model file not found at {self.model_path}")

        ext = self.model_path.suffix.lower()
        
        if ext == ".onnx":
            import onnxruntime as ort
            self.model = ort.InferenceSession(str(self.model_path))
            self.model_type = "onnx"
        elif ext in [".tflite"]:
            try:
                import tflite_runtime.interpreter as tflite
            except ImportError:
                from tensorflow import lite as tflite
            self.model = tflite.Interpreter(model_path=str(self.model_path))
            self.model.allocate_tensors()
            self.model_type = "tflite"
        elif ext in [".pkl", ".joblib"]:
            import joblib
            self.model = joblib.load(self.model_path)
            self.model_type = "sklearn"
        elif ext in [".pt", ".pth"]:
            import torch
            self.model = torch.jit.load(str(self.model_path))
            self.model.eval()
            self.model_type = "torch"
        else:
            raise ValueError(f"Unsupported model format: {ext}")

        logger.info(f"Loaded {self.model_type.upper()} model from {self.model_path.name}")

    def extract_features(self, record: MeasurementRecord) -> np.ndarray:
        """Transforms raw S-parameters into model input feature vectors."""
        eps = 1e-12
        if self.feature_type == "magnitude_db":
            s11_db = 20 * np.log10(np.abs(record.s11) + eps)
            s21_db = 20 * np.log10(np.abs(record.s21) + eps)
            features = np.concatenate([s11_db, s21_db])
        elif self.feature_type == "complex_iq":
            features = np.concatenate([
                np.real(record.s11), np.imag(record.s11),
                np.real(record.s21), np.imag(record.s21)
            ])
        else:  # Magnitude + Phase
            features = np.concatenate([
                np.abs(record.s11), np.angle(record.s11),
                np.abs(record.s21), np.angle(record.s21)
            ])
        
        return features.astype(np.float32).reshape(1, -1)

    def evaluate(self, record: MeasurementRecord) -> tuple[float, bool]:
        """
        Runs inference on the sweep record.
        Returns: (anomaly_score [0.0 to 1.0], is_anomaly [bool])
        """
        if self.model is None:
            return 0.0, False

        features = self.extract_features(record)

        try:
            if self.model_type == "onnx":
                input_name = self.model.get_inputs()[0].name
                outputs = self.model.run(None, {input_name: features})
                score = float(outputs[0][0])
            elif self.model_type == "tflite":
                input_details = self.model.get_input_details()
                output_details = self.model.get_output_details()
                self.model.set_tensor(input_details[0]['index'], features)
                self.model.invoke()
                score = float(self.model.get_tensor(output_details[0]['index'])[0])
            elif self.model_type == "sklearn":
                if hasattr(self.model, "predict_proba"):
                    score = float(self.model.predict_proba(features)[0][1])
                elif hasattr(self.model, "decision_function"):
                    score = float(self.model.decision_function(features)[0])
                else:  # Autoencoders / IsolationForest
                    score = float(-self.model.score_samples(features)[0])
            elif self.model_type == "torch":
                import torch
                with torch.no_grad():
                    tensor_in = torch.from_numpy(features)
                    score = float(self.model(tensor_in)[0].item())

            is_anomaly = score >= self.threshold
            return score, is_anomaly

        except Exception as e:
            logger.error(f"Error during ML inference: {e}")
            return 0.0, False