def get_password(username):
    return "hashed_" + username


def verify_password(hashed, password):
    return hashed == password


def login(username, password):
    stored = get_password(username)
    return verify_password(stored, password)
