#!/usr/bin/env python
# Purpose: Ros node to detect objects using tensorflow

import os, sys
import cv2
import numpy as np
import tensorflow as tf

# ROS related imports
import rospy
from std_msgs.msg import String, Header, Float32
from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError
from vision_msgs.msg import Detection2D, Detection2DArray, ObjectHypothesisWithPose
from tensorflow_object_detector.msg import pos_status

# Object detection module imports
import object_detection
from object_detection.utils import label_map_util
from object_detection.utils import visualization_utils as vis_util

# SET FRACTION OF GPU YOU WANT TO USE HERE
GPU_FRACTION = 0.4

######### Set model here ############
MODEL_NAME = 'ssd_mobilenet_v1_0.75_depth_300x300_coco14_sync_2018_07_03'

# By default models are stored in data/models/
MODEL_PATH = os.path.join(os.path.dirname(sys.path[0]), 'data', 'models', MODEL_NAME)

# Path to frozen detection graph. This is the actual model that is used for the object detection.
PATH_TO_CKPT = MODEL_PATH + '/frozen_inference_graph.pb'

######### Set the label map file here ###########
LABEL_NAME = 'mscoco_label_map.pbtxt'

# By default label maps are stored in data/labels/
PATH_TO_LABELS = os.path.join(os.path.dirname(
    sys.path[0]), 'data', 'labels', LABEL_NAME)

######### Set the number of classes here #########
NUM_CLASSES = 90

detection_graph = tf.Graph()
with detection_graph.as_default():
    od_graph_def = tf.GraphDef()
    with tf.gfile.GFile(PATH_TO_CKPT, 'rb') as fid:
        serialized_graph = fid.read()
        od_graph_def.ParseFromString(serialized_graph)
        tf.import_graph_def(od_graph_def, name='')

# Loading label map
# Label maps map indices to category names, so that when our convolution network predicts `5`,
# we know that this corresponds to `airplane`.  Here we use internal utility functions,
# but anything that returns a dictionary mapping integers to appropriate string labels would be fine
label_map = label_map_util.load_labelmap(PATH_TO_LABELS)
categories = label_map_util.convert_label_map_to_categories(label_map, max_num_classes=NUM_CLASSES, use_display_name=True)
category_index = label_map_util.create_category_index(categories)

# Setting the GPU options to use fraction of gpu that has been set
config = tf.ConfigProto()
config.gpu_options.per_process_gpu_memory_fraction = GPU_FRACTION

# Detection


class Detector:

    def __init__(self, alti_adj = False):

        self.image_pub = rospy.Publisher("debug_image", Image, queue_size=1)
        self.object_pub = rospy.Publisher("objects", Detection2DArray, queue_size=1)
        self.bridge = CvBridge()
        self.image_sub = rospy.Subscriber("image", Image, self.image_cb, queue_size=1, buff_size=2**24)   

        # collect drone position data (the distance to the object, the collision prob)
        # and action data (steering angle)
        self.pos_act_pub = rospy.Publisher("pos_act_params", pos_status, queue_size=1)
        self.pos_act = pos_status()
        
        self.line_z = rospy.Publisher("line_z", Float32, queue_size=1)   
        self.alti_adj = alti_adj
        self.sess = tf.Session(graph=detection_graph, config=config)
        

    def image_cb(self, data):
        objArray = Detection2DArray()
        try:
            cv_image = self.bridge.imgmsg_to_cv2(data, "bgr8")
        except CvBridgeError as e:
            print(e)
        image = cv2.cvtColor(cv_image, cv2.COLOR_BGR2RGB)

        # the array based representation of the image will be used later in order to prepare the
        # result image with boxes and labels on it.
        image_np = np.asarray(image)
        # Expand dimensions since the model expects images to have shape: [1, None, None, 3]
        image_np_expanded = np.expand_dims(image_np, axis=0)
        image_tensor = detection_graph.get_tensor_by_name('image_tensor:0')
        # Each box represents a part of the image where a particular object was detected.
        boxes = detection_graph.get_tensor_by_name('detection_boxes:0')
        # Each score represent how level of confidence for each of the objects.
        # Score is shown on the result image, together with the class label.
        scores = detection_graph.get_tensor_by_name('detection_scores:0')
        classes = detection_graph.get_tensor_by_name('detection_classes:0')
        num_detections = detection_graph.get_tensor_by_name('num_detections:0')

        (boxes, scores, classes, num_detections) = self.sess.run([boxes, scores, classes, num_detections],
                                                                 feed_dict={image_tensor: image_np_expanded})

        objects = vis_util.visualize_boxes_and_labels_on_image_array(
            image,
            np.squeeze(boxes),
            np.squeeze(classes).astype(np.int32),
            np.squeeze(scores),
            category_index,
            use_normalized_coordinates=True,
            line_thickness=2)

        # -----------------------------------------------------
        # publish information process
        # -----------------------------------------------------

        # initialization
        min_distance = 1
        coll_prob = 0
        steer_angle = 0
        line_z_vel = 0

        left_x = 0.5 # the left_x coordinate of the closest box
        right_x = 0.5
        max_y = 0.
        scale_percent = 120

        for i, b in enumerate(boxes[0]):
            if scores[0][i] >= 0.4:
                mid_x = (boxes[0][i][1] + boxes[0][i][3]) / 2
                mid_y = (boxes[0][i][0] + boxes[0][i][2]) / 2
                apx_distance = round(((1 - (boxes[0][i][3] - boxes[0][i][1])) ** 4), 1)
                # find the closest object
                if apx_distance <= min_distance:
                    left_x = boxes[0][i][1]
                    right_x = boxes[0][i][3]
                    min_distance = apx_distance
                    min_dis_index_x = mid_x
                    max_y = boxes[0][i][2]
        
        self.pos_act.dis_to_obj = min_distance            
        if max_y * 100 / scale_percent  <= 0.4:
            coll_prob = 0.
        else:
            coll_prob = 1 - min_distance
        self.pos_act.coll_prob = coll_prob

        if min_distance <= 0.5:
            # initialize linear_z_velocity
            line_z_vel = 0

            # if the object ahead is too large beween [0.2, 0.8] of the image, fly higher
            if self.alti_adj:
                if left_x <= 0.2 and right_x >= 0.8 and min_dis_index_y >= 0.6:
                    line_z_vel = 1.
                    steer_angle = 0.

            # else change direction
            else:
                if 0.3 <= min_dis_index_x <= 0.5:
                    steer_angle = -2.5 * min_dis_index_x + 0.25 
                elif 0.5 < min_dis_index_x <= 0.8:
                    steer_angle = -2.5 * min_dis_index_x + 2.25

        self.line_z.publish(line_z_vel)
        self.pos_act.steer = steer_angle

        self.pos_act_pub.publish(self.pos_act)

        objArray.detections = []
        objArray.header = data.header
        object_count = 1

        for i in range(len(objects)):
            object_count += 1
            objArray.detections.append(self.object_predict(
                objects[i], data.header, image_np, cv_image))

        self.object_pub.publish(objArray)

        img = cv2.cvtColor(image_np, cv2.COLOR_BGR2RGB)
         # percent of original size
        width = int(img.shape[1] * scale_percent / 100)
        height = int(img.shape[0] * scale_percent / 100)
        dim = (width, height)
        # resize image
        img = cv2.resize(img, dim, interpolation = cv2.INTER_AREA)

        image_out = Image()
        try:
            image_out = self.bridge.cv2_to_imgmsg(img, "bgr8")
        except CvBridgeError as e:
            print(e)
        image_out.header = data.header
        self.image_pub.publish(image_out)

    def object_predict(self, object_data, header, image_np, image):
        image_height, image_width, channels = image.shape
        obj = Detection2D()
        obj_hypothesis = ObjectHypothesisWithPose()

        object_id = object_data[0]
        object_score = object_data[1]
        dimensions = object_data[2]

        obj.header = header
        obj_hypothesis.id = object_id
        obj_hypothesis.score = object_score
        obj.results.append(obj_hypothesis)
        obj.bbox.size_y = int((dimensions[2]-dimensions[0])*image_height)
        obj.bbox.size_x = int((dimensions[3]-dimensions[1])*image_width)
        obj.bbox.center.x = int((dimensions[1] + dimensions[3])*image_height/2)
        obj.bbox.center.y = int((dimensions[0] + dimensions[2])*image_width/2)

        return obj


def main(args):
    rospy.init_node('detector_node')
    obj = Detector()
    try:
        rospy.spin()
    except KeyboardInterrupt:
        print("<---ShutDown---> CLOSE DETECTION")
    cv2.destroyAllWindows()


if __name__ == '__main__':
    main(sys.argv)
