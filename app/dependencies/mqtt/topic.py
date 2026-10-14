from queue import Queue
from .mqtt_functions import start_subscribe_thread
from json import loads
from logging import info

class Topic():
    '''a class to host topics and their associated information making
    it easier to control multiple topics from a list'''
    def __init__(self,broker:dict, details:dict):
        self.broker = broker
        self.name = details["name"]
        self.topic = details["topic"]
        self.is_subscribe = details["is_subscribe"]
        self.is_trigger = details["is_trigger"]
        self.queue = Queue()
        self.thread = None
        self.timeout = details.get("timout", 10)
        if self.is_subscribe:
            self.__create_listner()

    def __create_listner(self):
        '''creates a listner thread and queue'''
        self.thread = start_subscribe_thread(
            self.broker["mqtt_ip"], 
            self.broker["mqtt_port"], 
            self.topic, 
            self.queue,
            None
        )


def check_for_messages(topic:Topic):
    '''Checks the queue for each of the trigger topics and returns the message when any of them have received one
    Args:
        triggers: a dictionary of topics
        is_blocking: a boolean value that controls the blocking functionality
        timout: a float that determines the timout in s
    Returns:
        message: the message received from the trigger as dictionary'''

    if not topic:
        raise ValueError("Error : trigger cannot be empty")
    message = {"image": None}

    if topic.is_subscribe and not topic.is_trigger:
        try:
            message = loads(topic.queue.get(block = True, timeout=topic.timeout))
            return message
        except Exception as e:
            if e != KeyError: info(f"error occured when checking for trigger {e}")
    
    try:
        message = loads(topic.queue.get_nowait())
    except Exception as e:
        if e != KeyError: info(f"error occured when checking for trigger {e}")

    return message
