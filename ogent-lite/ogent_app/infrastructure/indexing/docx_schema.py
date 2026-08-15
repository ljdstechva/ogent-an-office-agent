"""WordprocessingML namespaces shared by DOCX indexing components."""

from __future__ import annotations

from .common import OFFICE_RELATIONSHIP_NAMESPACE


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
V = "urn:schemas-microsoft-com:vml"
R = OFFICE_RELATIONSHIP_NAMESPACE
NS = {
    "w": W,
    "w14": W14,
    "a": A,
    "c": C,
    "wp": WP,
    "m": M,
    "v": V,
    "r": R,
}
