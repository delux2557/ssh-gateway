"""Allow ``python -m ssh_gateway`` without installing the package."""

from .cli import main

raise SystemExit(main())
