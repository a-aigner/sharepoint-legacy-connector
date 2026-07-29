"""spconnect — read-only extraction connector for legacy on-premises SharePoint.

Targets Windows SharePoint Services 2.0/3.0 and MOSS 2007 through the classic
ASMX SOAP endpoints under ``_vti_bin/``. There is deliberately no CSOM, REST,
OData or Graph code anywhere in this package: those APIs do not exist on the
target farm.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
