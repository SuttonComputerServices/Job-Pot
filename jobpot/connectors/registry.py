"""Every job site Job Pot knows, in the order the Job sites page shows them."""
from .base import Connector
from .greenhouse import GREENHOUSE
from .lever import LEVER
from .linkedin import LINKEDIN
from .ziprecruiter import ZIPRECRUITER

REGISTRY: dict[str, Connector] = {c.key: c for c in (LINKEDIN, GREENHOUSE, LEVER, ZIPRECRUITER)}
