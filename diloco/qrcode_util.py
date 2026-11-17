"""Optional QR rendering for join codes (no hard dependency)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


def render_qr_svg(text: str) -> Optional[str]:
    """Return an SVG string for *text*, or None if no QR backend is available."""
    qr_bin = shutil.which("qr")
    if qr_bin:
        import tempfile

        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "join.svg"
                proc = subprocess.run(
                    [qr_bin, "--factory=svg", "--error-correction=L", f"--output={out}"],
                    input=text,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if proc.returncode == 0 and out.is_file():
                    svg = out.read_text(encoding="utf-8")
                    if svg.strip().startswith("<"):
                        return svg
        except Exception:
            pass
    try:
        import qrcode  # type: ignore[import-not-found]
        import qrcode.image.svg  # type: ignore[import-not-found]
        from io import StringIO

        buf = StringIO()
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        img.save(buf)
        return buf.getvalue()
    except Exception:
        return None


def write_qr(text: str, path: Path) -> bool:
    svg = render_qr_svg(text)
    if not svg:
        return False
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")
    return True
