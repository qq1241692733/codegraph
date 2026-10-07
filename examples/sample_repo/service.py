from auth import login
from models import User


def helper(user):
    return f"helped:{user.name}"


class Service:
    def create_user(self, name, password):
        u = User(name)
        login(name, password)
        return helper(u)
