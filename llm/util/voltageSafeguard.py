def voltage_selection_filter(d):
    """Collapse a live `voltageV` reading down to a droplet-selection signal.
 
    Walks the same dict/list shape live_status() (or an already-filtered live
    payload) returns, exactly like llm_context() does. Wherever it finds the
    raw "voltageV" key, it replaces it with "voltageInWorkingRange": True if
    the reading falls inside the 150-250 V working-voltage band from the
    droplet-selection criteria, False otherwise. If the value is missing or
    not numeric, the key is dropped rather than guessed.
 
    The raw voltage is never returned in any form -- not passed through,
    not rounded, not bucketed more finely than this one boolean.
    """
    if isinstance(d, list):
        return [voltage_selection_filter(x) for x in d]
    if not isinstance(d, dict):
        return d
 
    out = {}
    for k, v in d.items():
        if k == "voltageV":
            if isinstance(v, (int, float)):
                out["voltageInWorkingRange"] = 150 <= v <= 250
            # missing / non-numeric reading: omit rather than fabricate
            continue
        out[k] = voltage_selection_filter(v)
    return out