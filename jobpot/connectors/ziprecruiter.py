from .base import Connector

ZIPRECRUITER = Connector(
    key="ziprecruiter", label="ZipRecruiter", can_discover=False, can_apply=False,
    risk_note="ZipRecruiter blocks automated visitors, so Job Pot can't search or apply there. "
              "Add ZipRecruiter jobs by hand to track them.",
)
