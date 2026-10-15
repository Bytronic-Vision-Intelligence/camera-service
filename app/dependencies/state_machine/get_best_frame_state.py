from .base_functions.state import State
from .publish_state import PublishState
from cv2 import split, calcHist, cvtColor, COLOR_RGB2GRAY
from numpy import ndarray, mean, uint8, uint16, int32, float32, float64
from math import inf
class GetBestFrameState(State):
    ''''''
    def __init__(self, state_machine, frames:list[dict], state_id = "Get Best Frame"):
        super().__init__(state_machine, state_id)
        self.frames = frames
        dtype_to_bits = {
            uint8: 8,
            uint16: 16,
            int32: 32,
            float32: 32,
            float64: 64
        }
        self.mid_value = pow(2,dtype_to_bits.get(frames[0].get("image").dtype.type, None))/2

    def tick(self):
        ''''''
        histogram_score = []
        best_score = [0,inf]
        for i in range(0, len(self.frames)):
            image_histograms = self.__get_histogram(self.frames[i].get("image"))
            mean_intensity = self.__histogram_mean(
                image_histograms.get("greyscale")
            )
            score = abs(mean_intensity - self.mid_value)
            histogram_score.append(score)
            if score < best_score[1]:
                best_score = [i,score]

        self.my_state_machine.change_state(
            PublishState(
                self.my_state_machine,
                self.frames[best_score[0]]
            )
        )

    def __histogram_mean(self,histogram):
        total = sum(histogram)
        if total == 0:
            return 0.0  # or raise ValueError("Histogram is empty")
        return sum(i * count for i, count in enumerate(histogram)) / total
        
    def __get_histogram(self, image:ndarray):
        '''creates a histogram of an image and each channel then outputs the
        result as a dictionary of images'''
        histogram = {}
        grey_image = cvtColor(image, COLOR_RGB2GRAY)
        histogram["greyscale"] = calcHist([grey_image],[0],None,[256],[0,256])
        if image.shape[2] < 1: return histogram
        r,g,b = split(image)
        histogram["red"] = calcHist([r],[0],None,[256],[0,256])
        histogram["green"] = calcHist([g],[0],None,[256],[0,256])
        histogram["blue"] = calcHist([b],[0],None,[256],[0,256])

        return histogram