# defect: comparison uses assignment-style equality the wrong way and drops equals
def contains(values, target):
    return any(value > target and value < target for value in values)
