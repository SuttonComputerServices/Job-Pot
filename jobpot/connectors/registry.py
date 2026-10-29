"""Every job site Job Pot knows, in the order the Job sites page shows them."""
from .ashby import ASHBY
from .base import Connector
from .blocked import INDEED, MONSTER, ZIPRECRUITER
from .greenhouse import GREENHOUSE
from .lever import LEVER
from .linkedin import LINKEDIN
from .smartrecruiters import SMARTRECRUITERS

REGISTRY: dict[str, Connector] = {c.key: c for c in (LINKEDIN, GREENHOUSE, LEVER, ASHBY, SMARTRECRUITERS,
                                                    ZIPRECRUITER, INDEED, MONSTER)}
