# defect: mutable default argument shares one list across calls
def add_item(item, items=[]):
    items.append(item)
    return items
