"""PresentationML namespaces shared by PPTX indexing components."""

from __future__ import annotations

from .common import OFFICE_RELATIONSHIP_NAMESPACE


P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
R = OFFICE_RELATIONSHIP_NAMESPACE
NS = {"p": P, "a": A, "c": C, "dgm": DGM, "r": R}
