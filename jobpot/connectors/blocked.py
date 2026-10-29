"""Sites that block automated visitors: listed so their jobs can be added by hand and tracked."""
from .base import Connector


def _blocked(key: str, label: str) -> Connector:
    return Connector(
        key=key, label=label, can_discover=False, can_apply=False,
        risk_note=f"{label} blocks automated visitors, so Job Pot can't search or apply there. "
                  f"Add {label} jobs by hand to track them.",
    )


ZIPRECRUITER = _blocked("ziprecruiter", "ZipRecruiter")
INDEED = _blocked("indeed", "Indeed")
MONSTER = _blocked("monster", "Monster")
