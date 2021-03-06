import time
import os
import logging
from threading import Thread
from urllib.parse import urljoin
import speech_recognition as sr
import requests
import json_config
from speech_recognition import Recognizer, Microphone
from requests.exceptions import ConnectionError
import queue

import susi_python as susi
from .lights import lights
from .internet_test import internet_on
from ..scheduler import ActionScheduler
from ..player import player
from ..config import susi_config
from ..speech import TTS

logger = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO
except:
    logger.warning("This device doesn't have GPIO port")
    GPIO = None

class Components:
    """Common components accessible by each state of the the  SUSI state Machine.
    """

    def __init__(self, renderer=None):
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(27, GPIO.OUT)
            GPIO.setup(22, GPIO.OUT)
        except ImportError:
            logger.warning("This device doesn't have GPIO port")
        except RuntimeError as e:
            logger.error(e)
            pass
        thread1 = Thread(target=self.server_checker, name="Thread1")
        thread1.daemon = True
        thread1.start()

        recognizer = Recognizer()
        recognizer.dynamic_energy_threshold = False
        recognizer.energy_threshold = 1000
        self.recognizer = recognizer
        self.microphone = Microphone()
        self.susi = susi
        self.renderer = renderer
        self.server_url = "https://127.0.0.1:4000"
        self.action_schduler = ActionScheduler()
        self.action_schduler.start()

        try:
            res = requests.get('http://ip-api.com/json').json()
            self.susi.update_location(
                longitude=res['lon'], latitude=res['lat'],
                country_name=res['country'], country_code=res['countryCode'])

        except ConnectionError as e:
            logger.error(e)

        self.config = json_config.connect('config.json')

        if self.config['usage_mode'] == 'authenticated':
            try:
                susi.sign_in(email=self.config['login_credentials']['email'],
                             password=self.config['login_credentials']['password'])
            except Exception as e:
                logger.error('Some error occurred in login. Check you login details in config.json.\n%s', e)

        if self.config['hotword_engine'] == 'Snowboy':
            from ..hotword_engine.snowboy_detector import SnowboyDetector
            self.hotword_detector = SnowboyDetector()
        else:
            from ..hotword_engine.sphinx_detector import PocketSphinxDetector
            self.hotword_detector = PocketSphinxDetector()

        if self.config['WakeButton'] == 'enabled':
            logger.info("Susi has the wake button enabled")
            if self.config['Device'] == 'RaspberryPi':
                logger.info("Susi runs on a RaspberryPi")
                from ..hardware_components import RaspberryPiWakeButton
                self.wake_button = RaspberryPiWakeButton()
            else:
                logger.warning("Susi is not running on a RaspberryPi")
                self.wake_button = None
        else:
            logger.warning("Susi has the wake button disabled")
            self.wake_button = None

    def server_checker(self):
        response_one = None
        test_params = {
            'q': 'Hello',
            'timezoneOffset': int(time.timezone / 60)
        }
        while response_one is None:
            try:
                logger.debug("checking for local server")
                url = urljoin(self.server_url, '/susi/chat.json')
                response_one = requests.get(url, test_params).result()
                api_endpoint = self.server_url
                susi.use_api_endpoint(api_endpoint)
            except AttributeError:
                time.sleep(10)
                continue
            except ConnectionError:
                time.sleep(10)
                continue



class SusiStateMachine():
    """Actually not a state machine, but we keep the name for now"""

    def __init__(self, renderer=None):
        self.components = Components(renderer)
        self.event_queue = queue.Queue()

        if self.components.hotword_detector is not None:
            self.components.hotword_detector.subject.subscribe(
                on_next=lambda x: self.hotword_detected_callback())
        if self.components.wake_button is not None:
            self.components.wake_button.subject.subscribe(
                on_next=lambda x: self.hotword_detected_callback())
        if self.components.renderer is not None:
            self.components.renderer.subject.subscribe(
                on_next=lambda x: self.hotword_detected_callback())
        if self.components.action_schduler is not None:
            self.components.action_schduler.subject.subscribe(
                on_next=lambda x: self.queue_event(x))

    def queue_event(self,event):
        self.event_queue.put(event)


    def start(self):
        while True:
            logger.debug("starting detector")
            if self.event_queue.empty():
                self.start_detector()
            else:
                ev = self.event_queue.get()
                self.deal_with_answer(ev)
            logger.debug("after starting detector")
            # back from processing
            player.restore_softvolume()
            if GPIO:
                try:
                    GPIO.output(27, False)
                    GPIO.output(22, False)
                except RuntimeError:
                    pass


    def notify_renderer(self, message, payload=None):
        if self.components.renderer is not None:
            self.components.renderer.receive_message(message, payload)

    def start_detector(self):
        self.components.hotword_detector.start()

    def stop_detector(self):
        self.components.hotword_detector.stop()


    def hotword_detected_callback(self):
        # beep
        player.beep(os.path.abspath(os.path.join(self.components.config['data_base_dir'],
                                                 self.components.config['detection_bell_sound'])))
        # stop hotword detection
        logger.debug("stopping hotword detector")
        self.stop_detector()
        if GPIO:
            GPIO.output(22, True)
        audio = None
        logger.debug("notify renderer for listening")
        self.notify_renderer('listening')
        recognizer = self.components.recognizer
        with self.components.microphone as source:
            try:
                logger.debug("listening to voice command")
                audio = recognizer.listen(source, timeout=10.0, phrase_time_limit=5)
            except sr.WaitTimeoutError:
                logger.debug("timeout reached waiting for voice command")
                self.deal_with_error('ListenTimeout')
                return
        if GPIO:
            GPIO.output(22, False)

        lights.off()
        lights.think()
        try:
            logger.debug("Converting audio to text")
            value = self.recognize_audio(audio=audio, recognizer=recognizer)
            logger.debug("recognize_audio => %s", value)
            self.notify_renderer('recognized', value)
            if self.deal_with_answer(value):
                pass
            else:
                logger.error("Error dealing with answer")

        except sr.UnknownValueError as e:
            logger.error("UnknownValueError from SpeechRecognition: %s", e)
            self.deal_with_error('RecognitionError')

        return

    def __speak(self, text):
        """Method to set the default TTS for the Speaker
        """
        if self.components.config['default_tts'] == 'google':
            TTS.speak_google_tts(text)
        if self.components.config['default_tts'] == 'flite':
            logger.info("Using flite for TTS")  # indication for using an offline music player
            TTS.speak_flite_tts(text)
        elif self.components.config['default_tts'] == 'watson':
            TTS.speak_watson_tts(text)

    def recognize_audio(self, recognizer, audio):
        logger.info("Trying to recognize audio with %s in language: %s", self.components.config['default_stt'], susi_config["language"])
        if self.components.config['default_stt'] == 'google':
            return recognizer.recognize_google(audio, language=susi_config["language"])

        elif self.components.config['default_stt'] == 'watson':
            username = self.components.config['watson_stt_config']['username']
            password = self.components.config['watson_stt_config']['password']
            return recognizer.recognize_ibm(
                username=username,
                password=password,
                language=susi_config["language"],
                audio_data=audio)
        elif self.components.config['default_stt'] == 'pocket_sphinx':
            lang = susi_config["language"].replace("_", "-")
            if internet_on():
                self.components.config['default_stt'] = 'google'
                return recognizer.recognize_google(audio, language=lang)
            else:
                return recognizer.recognize_sphinx(audio, language=lang)

        elif self.components.config['default_stt'] == 'bing':
            api_key = self.components.config['bing_speech_api_key']
            return recognizer.recognize_bing(audio_data=audio, key=api_key, language=susi_config["language"])

        elif self.components.config['default_stt'] == 'deepspeech-local':
            lang = susi_config["language"].replace("_", "-")
            return recognizer.recognize_deepspeech(audio, language=lang)


    def deal_with_error(self, payload=None):
        if payload == 'RecognitionError':
            logger.debug("ErrorState Recognition Error")
            self.notify_renderer('error', 'recognition')
            lights.speak()
            player.say(os.path.abspath(os.path.join(self.components.config['data_base_dir'],
                                                    self.components.config['recognition_error_sound'])))
            lights.off()
        elif payload == 'ConnectionError':
            self.notify_renderer('error', 'connection')
            config['default_tts'] = 'flite'
            config['default_stt'] = 'pocket_sphinx'
            print("Internet Connection not available")
            lights.speak()
            lights.off()
            logger.info("Changed to offline providers")

        elif payload == 'ListenTimeout':
            self.notify_renderer('error', 'timeout')
            # TODO make a Tada sound here
            lights.speak()
            lights.off()

        else:
            print("Error: {} \n".format(payload))
            self.notify_renderer('error')
            lights.speak()
            player.say(os.path.abspath(os.path.join(self.components.config['data_base_dir'],
                                                    self.components.config['problem_sound'])))
            lights.off()


    def deal_with_answer(self, payload=None):
        try:
            no_answer_needed = False

            if isinstance(payload, str):
                logger.debug("Sending payload to susi server: %s", payload)
                reply = self.components.susi.ask(payload)
            else:
                logger.debug("Executing planned action response", payload)
                reply = payload

            if GPIO:
                GPIO.output(27, True)
            if self.components.renderer is not None:
                self.notify_renderer('speaking', payload={'susi_reply': reply})

            if 'planned_actions' in reply.keys():
                logger.debug("planning action: ")
                for plan in reply['planned_actions']:
                    logger.debug("plan = " + str(plan))
                    # TODO TODO
                    # plan_delay is wrong, it is 0, we need to use
                    # plan = {'language': 'en', 'answer': 'ALARM', 'plan_delay': 0, 'plan_date': '2019-12-30T13:36:05.458Z'}
                    # plan_date !!!!!
                    self.components.action_schduler.add_event(int(plan['plan_delay']) / 1000,
                                                              plan)

            # first responses WITHOUT answer key!

            # {'answer': 'Audio volume is now 10 percent.', 'volume': '10'}
            if 'volume' in reply.keys():
                no_answer_needed = True
                player.volume(reply['volume'])
                player.say(os.path.abspath(os.path.join(self.components.config['data_base_dir'],
                                                        self.components.config['detection_bell_sound'])))

            if 'media_action' in reply.keys():
                action = reply['media_action']
                if action == 'pause':
                    no_answer_needed = True
                    player.pause()
                    lights.off()
                    lights.wakeup()
                elif action == 'resume':
                    no_answer_needed = True
                    player.resume()
                elif action == 'restart':
                    no_answer_needed = True
                    player.restart()
                elif action == 'next':
                    no_answer_needed = True
                    player.next()
                elif action == 'previous':
                    no_answer_needed = True
                    player.previous()
                elif action == 'shuffle':
                    no_answer_needed = True
                    player.shuffle()
                else:
                    logger.error('Unknown media action: %s', action)

            # {'stop': <susi_python.models.StopAction object at 0x7f4641598d30>}
            if 'stop' in reply.keys():
                no_answer_needed = True
                player.stop()

            if 'answer' in reply.keys():
                logger.info('Susi: %s', reply['answer'])
                lights.off()
                lights.speak()
                self.__speak(reply['answer'])
                lights.off()
            else:
                if not no_answer_needed and 'identifier' not in reply.keys():
                    lights.off()
                    lights.speak()
                    self.__speak("I don't have an answer to this")
                    lights.off()

            if 'language' in reply.keys():
                answer_lang = reply['language']
                if answer_lang != susi_config["language"]:
                    logger.info("Switching language to: %s", answer_lang)
                    # switch language
                    susi_config["language"] = answer_lang

            # answer to "play ..."
            # {'identifier': 'ytd-04854XqcfCY', 'answer': 'Playing Queen -  We Are The Champions (Official Video)'}
            if 'identifier' in reply.keys():
                url = reply['identifier']
                logger.debug("Playing " + url)
                if url[:3] == 'ytd':
                    player.playytb(url[4:])
                else:
                    player.play(url)

            if 'table' in reply.keys():
                table = reply['table']
                for h in table.head:
                    print('%s\t' % h, end='')
                    self.__speak(h)
                print()
                for datum in table.data[0:4]:
                    for value in datum:
                        print('%s\t' % value, end='')
                        self.__speak(value)
                    print()

            if 'rss' in reply.keys():
                rss = reply['rss']
                entities = rss['entities']
                count = rss['count']
                for entity in entities[0:count]:
                    logger.debug(entity.title)
                    self.__speak(entity.title)

        except ConnectionError:
            return self.to_error('ConnectionError')
        except Exception as e:
            logger.error('Got error: %s', e)
            return False

        return True


