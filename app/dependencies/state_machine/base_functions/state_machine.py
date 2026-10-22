from dependencies.mqtt.topic import Topic
from dependencies.mqtt.mqtt_functions import MQTTConfig, MQTTClient
class StateMachine():
    '''a class containing state machine functionality'''
    def __init__(self, config:dict):
        self.service_id = config.get("service_id", "camera_1")
        self.mqtt_client = self.__create_mqtt_client(config)
        self.topics = self.__create_topic_listeners(config)
        self.trigger_request = self.get_topic(self.topics,"trigger")
        self.service_output = self.get_topic(self.topics, "image")
        self.blocking_subscribers = self.__get_blocking_subscribers(self.topics)
        self.state = self.set_idle()
        pass

    def change_state(self, state):
        '''changes the state and puts the new state into its run loop'''
        self.state.exit()
        self.state = state
        self.state.enter()

    def runtime(self):
        ''''''
        self.state.tick()

    def set_idle(self):
        from dependencies.state_machine.idle_state import IdleState
        return IdleState(self)

    @staticmethod
    def __create_topic_listeners(config:dict):
        '''creates a set of listeners for the given topics and adds
        qeueue objects to the dictionary as well as reference to the thread
        '''
        broker = config.get('mqtt')
        topics = broker.get("topics")
        topic_list = {}
        for topic in topics:
            topic_new = Topic(broker, topic)
            topic_list[f"{topic.get('name')}"] = topic_new
        return topic_list

    @staticmethod
    def __create_mqtt_client(config:dict):
        '''creates an mqtt client object from the config'''
        broker_details = config.get("mqtt")
        
        mqtt_config = MQTTConfig(
            host=broker_details["mqtt_ip"], 
            port=broker_details["mqtt_port"]
        )
        client = MQTTClient(mqtt_config)
        client.connect()
        return client

    @staticmethod
    def get_topic(topic_dict:dict, topic_name:str)-> Topic:
        '''returns the topic associated with the key value
        Args:
            topic_dict: a dictionary of topics, one value needs to be "name"
            topic_name: the name to search within the topic list
        Returns:
            the topic object accosiated with the name
        '''
        return topic_dict.get(topic_name)

    @staticmethod
    def __get_blocking_subscribers(topics):
        '''returns a list of topics that are not triggers and will block
        functionality until data is received
        Args:
            config: the microservice config containing a correctly formatted topics section
        returns
            a list of topics
        '''
        blocking_subscribers = []
        for topic in topics:
            topic = topics.get(topic)
            if not topic.is_subscribe or topic.is_trigger: continue
            blocking_subscribers.append(topic)
        return blocking_subscribers