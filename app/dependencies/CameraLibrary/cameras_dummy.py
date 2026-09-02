from dependencies.CameraLibrary.cameras import Camera
import cv2
import logging
from numpy import ndarray
from pathlib import Path
from random import randint
import os

class DummyCamera(Camera):
    '''A camera class used for testing with images from a file, not using any real camera'''

    def __init__(self, directory_path:Path, extension:str=".png"):
        self.camera = None
        self.cam = None

        self._get_frame_list(directory_path, extension)

    def connect_to_camera(self):        
        """ Connect to the camera based on the specified camera type.
        Raises:
            Exception: If the camera type is unsupported or if connection fails."""
        pass

    def _get_frame_list(self,directory_path:Path, extension:str=".png"):
        '''returns a list of images from a direcory
        Args:
            directory_path: the path to the directory
            extension: a string containing the image extension
        '''
        self.frame_list = []
        frame_path_list = []
        for root, dirs, files in os.walk(directory_path):
            for file in files:
                if file.endswith(extension):
                    self.frame_list.append(os.path.join(root, file))
    
    def capture_image(self, timeout_ms):
        """Returns a random frame from the current frame list.
        Returns:
            numpy.ndarray: The captured image.
        Raises:
            Exception: If the camera type is unsupported or if image capture fails."""
        
        return cv2.imread(self.frame_list[randint(0, len(self.frame_list)-1)])

    def disconnect_camera(self, camera=None) -> None:
        """Release the OpenCV capture. Subclasses typically override this."""
        pass
