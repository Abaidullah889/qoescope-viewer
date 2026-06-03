import os
import struct
import sys
import types
import unittest.mock as mock

# Path to your decoder.py inside the project
DECODER_PATH = os.path.join(os.path.dirname(__file__), "../../analyzer/decoder.py")


def _stub_heavy_deps():
    # av stub
    av_mod = types.ModuleType("av")
    av_mod.CodecContext = type("CodecContext", (), {
        "create": staticmethod(lambda *a, **kw: type("Ctx", (), {
            "decode":         lambda self, p: [],
            "flush_buffers":  lambda self: None,
        })())
    })
    av_mod.Packet = lambda data: data
    sys.modules["av"] = av_mod

    # cv2 stub
    cv2_mod = types.ModuleType("cv2")
    q_mod   = types.ModuleType("cv2.quality")
    q_mod.QualityBRISQUE_create = staticmethod(
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("stub"))
    )
    cv2_mod.quality = q_mod
    sys.modules["cv2"]         = cv2_mod
    sys.modules["cv2.quality"] = q_mod

    # numpy stub
    sys.modules["numpy"] = types.ModuleType("numpy")


def load_decoder() -> dict:
    _stub_heavy_deps()

    with open(DECODER_PATH, "r") as f:
        full_source = f.read()

    cut_pos = full_source.find("\nwhile True:")
    assert cut_pos > 0, (
        "Could not find 'while True:' in decoder.py"
    )
    source_funcs_only = full_source[:cut_pos]

    # Build a clean execution namespace
    ns = {
        "struct":       struct,
        "__name__":     "__test__",
        "__file__":     DECODER_PATH,
        "__builtins__": __builtins__,
    }

    # Patch socket.socket (prevents real UDP bind),
    # open() for the log file, makedirs for the metrics dir
    with mock.patch("socket.socket"), \
         mock.patch("builtins.open", mock.mock_open()), \
         mock.patch("os.makedirs"):
        exec(compile(source_funcs_only, DECODER_PATH, "exec"), ns)

    return ns