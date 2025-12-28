from flask_login import UserMixin

class User(UserMixin):
    def __init__(self, username):
        self.id = username
        self.username = username

def load_user(user_id):
    return User(user_id)
