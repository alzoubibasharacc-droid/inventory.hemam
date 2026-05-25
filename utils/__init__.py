def stock_status(current_qty, minimum_stock):
    """
    Evaluate stock level against the item's minimum_stock threshold.

    Returns:
        'critical'  — stock is zero or negative and a minimum is set
        'low'       — stock is positive but below minimum
        'ok'        — stock meets or exceeds minimum, or no minimum is set
    """
    ms = minimum_stock or 0.0
    if ms <= 0:
        return 'ok'
    if current_qty <= 0:
        return 'critical'
    if current_qty < ms:
        return 'low'
    return 'ok'
