from locust import HttpUser, task, LoadTestShape, events
from locust.exception import StopUser
import time

class TestUser(HttpUser):
    wait_time = lambda self: 0.1
    
    def on_start(self):
        self.start_time = time.time()
        
    @task
    def do_work(self):
        if time.time() - self.start_time > 2:
            print("User stopping")
            raise StopUser()

class MaintainShape(LoadTestShape):
    def tick(self):
        num_users = self.runner.environment.parsed_options.num_users or 10
        spawn_rate = self.runner.environment.parsed_options.spawn_rate or 5
        return (num_users, spawn_rate)
