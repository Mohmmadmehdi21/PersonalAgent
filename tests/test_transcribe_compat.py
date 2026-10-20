import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import av
    import transcribe
    HAS_TRANSCRIBE_DEPS = True
except ImportError:
    av = None
    transcribe = None
    HAS_TRANSCRIBE_DEPS = False


@unittest.skipUnless(HAS_TRANSCRIBE_DEPS, "PyAV and faster-whisper dependencies are required for PyAV compatibility tests.")
class PyAVCompatibilityTests(unittest.TestCase):
    def test_transcribe_adapts_removed_metadata_errors_keyword_on_pyav_19(self):
        calls = []

        # Simulate the PyAV 19 Cython callable: it accepts file and mode only.
        def pyav_19_open(file, mode="r"):
            calls.append((file, mode))
            return object()

        segment = SimpleNamespace(text="sample transcript", start=0.0, end=1.0, words=[])
        model = SimpleNamespace(transcribe=lambda path, **kwargs: (
            (av.open(path, mode="r", metadata_errors="ignore"), [segment])[1],
            SimpleNamespace(duration=0),
        ))

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            audio_path = Path(directory) / "sample.wav"
            audio_path.write_bytes(b"stub audio; decoder is mocked")
            with patch.object(transcribe, "is_valid_json_file", return_value=False), \
                 patch.object(transcribe, "WhisperModel", side_effect=[RuntimeError("no GPU"), model]), \
                 patch.object(av, "open", new=pyav_19_open), \
                 patch.object(av, "__version__", "19.0.1"):
                result = transcribe.transcribe(str(audio_path))

        self.assertEqual(result[0]["text"], "sample transcript")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "r")

    def test_older_pyav_keeps_explicit_metadata_error_policy(self):
        calls = []

        def pyav_18_open(file, mode="r", metadata_errors="strict"):
            calls.append((file, mode, metadata_errors))
            return object()

        with transcribe._pyav_open_compat(SimpleNamespace(__version__="18.0.0", open=pyav_18_open)):
            result = pyav_18_open("audio", mode="r", metadata_errors="ignore")
        self.assertIsNotNone(result)
        self.assertEqual(calls, [("audio", "r", "ignore")])


if __name__ == "__main__":
    unittest.main()
