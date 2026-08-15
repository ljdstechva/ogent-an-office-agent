"""SpreadsheetML namespaces and formula tokens shared by XLSX indexers."""

from __future__ import annotations

import re

from .common import OFFICE_RELATIONSHIP_NAMESPACE


S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = OFFICE_RELATIONSHIP_NAMESPACE
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
NS = {"x": S, "r": R, "c": C, "a": A, "xdr": XDR}
CELL_REFERENCE = re.compile(
    r"(?<![A-Z0-9_.])"
    r"(?:(?:'([^']+)'|([A-Za-z_][A-Za-z0-9_. ]*))!)?"
    r"(\$?[A-Z]{1,3}\$?[1-9][0-9]*)"
    r"(?::(\$?[A-Z]{1,3}\$?[1-9][0-9]*))?"
    r"(?![A-Z0-9_(])",
    re.IGNORECASE,
)
