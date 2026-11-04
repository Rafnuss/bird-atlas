"""Helpers for the selected eBird taxonomy reference."""


def reporting_taxa(codes, report_as):
    """Include each taxon's reporting parents, following the reference codes."""
    included = set(codes)
    pending = list(included)
    while pending:
        code = pending.pop()
        parent = report_as[code]
        if parent and parent not in included:
            included.add(parent)
            pending.append(parent)
    return included
