from service import Service


def run_app():
    svc = Service()
    svc.create_user("alice", "pw")
