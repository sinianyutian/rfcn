import chainer
from chainer import cuda
import chainer.functions as F
import chainer.links as L
from chainer import Variable
import cupy
import fcn
import numpy as np
import sklearn.metrics

from rfcn.external.faster_rcnn.faster_rcnn.proposal_target_layer \
    import ProposalTargetLayer
from rfcn.external.faster_rcnn.models.rpn import RPN
from rfcn import functions
from rfcn import utils


class FCIS(chainer.Chain):

    """FCIS based on pretrained model of VGG16."""

    def __init__(self, C, k=7):
        """Initialize network architecture.

        Parameters
        ----------
        C: int
            number of object categories except background.
        k: int (default: 7)
            kernel size for translation-aware score map.
        """
        super(FCIS, self).__init__()
        self.C = C
        self.k = k

        # feature extraction:
        self.add_link('conv1_1', L.Convolution2D(3, 64, 3, stride=1, pad=1))
        self.add_link('conv1_2', L.Convolution2D(64, 64, 3, stride=1, pad=1))
        self.add_link('conv2_1', L.Convolution2D(64, 128, 3, stride=1, pad=1))
        self.add_link('conv2_2', L.Convolution2D(128, 128, 3, stride=1, pad=1))
        self.add_link('conv3_1', L.Convolution2D(128, 256, 3, stride=1, pad=1))
        self.add_link('conv3_2', L.Convolution2D(256, 256, 3, stride=1, pad=1))
        self.add_link('conv3_3', L.Convolution2D(256, 256, 3, stride=1, pad=1))
        self.add_link('conv4_1', L.Convolution2D(256, 512, 3, stride=1, pad=1))
        self.add_link('conv4_2', L.Convolution2D(512, 512, 3, stride=1, pad=1))
        self.add_link('conv4_3', L.Convolution2D(512, 512, 3, stride=1, pad=1))

        # rpn:
        self.add_link('rpn', RPN(512, 512, n_anchors=9, feat_stride=16))
        self.proposal_target_layer = ProposalTargetLayer(C+1)

        # translation-aware instance inside/outside score map:
        # out_channel is 2 * k^2 * (C + 1): 2 is inside/outside,
        # k is kernel size, and (C + 1) is object categories and background.
        self.add_link('score_fr',
                      L.Convolution2D(512, 2 * k**2 * (C + 1), ksize=1))

    def _extract_feature(self, x):
        h = F.relu(self.conv1_1(x))
        h = F.relu(self.conv1_2(h))
        h = F.max_pooling_2d(h, 2, stride=2)  # 1/2

        h = F.relu(self.conv2_1(h))
        h = F.relu(self.conv2_2(h))
        h = F.max_pooling_2d(h, 2, stride=2)  # 1/4

        h = F.relu(self.conv3_1(h))
        h = F.relu(self.conv3_2(h))
        h = F.relu(self.conv3_3(h))
        h = F.max_pooling_2d(h, 2, stride=2)  # 1/8

        h = F.relu(self.conv4_1(h))
        h = F.relu(self.conv4_2(h))
        h = F.relu(self.conv4_3(h))
        # h = F.max_pooling_2d(h, 2, stride=2)

        return h  # 1/8

    def _propose_regions(self, x, lbl_cls, lbl_ins, h_feature, device):
        # gt_boxes: [[x1, y1, x2, y2, label], ...]
        gt_boxes = utils.label_to_bboxes(lbl_ins, ignore_label=(-1, 0))
        roi_clss = utils.label_rois(gt_boxes, lbl_ins, lbl_cls)[0]
        gt_boxes = np.hstack((gt_boxes, roi_clss.reshape((-1, 1))))
        # propose regions
        # im_info: [[height, width, image_scale], ...]
        height, width = x.shape[2:4]
        im_info = np.array([[height, width, 1]], dtype=np.float32)
        # loss_bbox_reg: bbox regression loss
        # rois: (n_rois, 5), [batch_index, x1, y1, x2, y2]
        loss_rpn_cls, loss_rpn_reg, rois = self.rpn(
            h_feature,
            im_info=im_info,
            gt_boxes=gt_boxes,
            gpu=device,
        )
        rois = self.proposal_target_layer(rois, gt_boxes)[0]
        rois = rois[:, 1:]  # [x1, y1, x2, y2]
        loss_rpn = loss_rpn_cls + loss_rpn_reg
        return loss_rpn, rois

    def __call__(self, x, lbl_cls, lbl_ins):
        xp = cuda.get_array_module(x.data)
        device = x.data.device.id

        lbl_cls = cuda.to_cpu(lbl_cls.data[0])
        lbl_ins = cuda.to_cpu(lbl_ins.data[0])

        self.x = x
        self.lbl_cls = lbl_cls
        self.lbl_ins = lbl_ins

        down_scale = 8.0
        h_feature = self._extract_feature(x)  # 1/8

        loss_rpn, rois = self._propose_regions(
            x, lbl_cls, lbl_ins, h_feature, device=device)
        rois_ns = (rois / down_scale).astype(np.int32)
        rois = rois.astype(np.int64)

        self.rois = rois

        # (1, 2*k^2*(C+1), height/down_scale, width/down_scale)
        h_score = self.score_fr(h_feature)  # 1/down_scale
        assert h_score.shape[:2] == (1, 2*self.k**2*(self.C+1))

        roi_clss, roi_segs = utils.label_rois(rois, lbl_ins, lbl_cls)

        loss_cls = Variable(xp.array(0, dtype=np.float32), volatile='auto')
        loss_seg = Variable(xp.array(0, dtype=np.float32), volatile='auto')
        n_loss_cls = 0
        n_loss_seg = 0

        n_rois = len(rois)

        roi_clss_pred = np.zeros((n_rois,), dtype=np.int32)
        cls_scores = np.zeros((n_rois, self.C+1), dtype=np.float32)
        roi_mask_probs = [None] * n_rois

        for i_roi in xrange(n_rois):
            roi_ns = rois_ns[i_roi]
            roi_cls = roi_clss[i_roi]
            roi_seg = roi_segs[i_roi]

            roi_cls_var = xp.array([roi_cls], dtype=np.int32)
            roi_cls_var = Variable(roi_cls_var, volatile='auto')

            x1, y1, x2, y2 = roi_ns
            roi_h = y2 - y1
            roi_w = x2 - x1

            if not (roi_h >= self.k and roi_w >= self.k):
                continue
            assert roi_h * roi_w > 0

            roi_score = h_score[:, :, y1:y2, x1:x2]
            assert roi_score.shape == (1, 2*self.k**2*(self.C+1), roi_h, roi_w)

            roi_score = functions.assemble_2d(roi_score, self.k)
            assert roi_score.shape == (1, 2*(self.C+1), roi_h, roi_w)

            roi_score = F.reshape(roi_score, (1, self.C+1, 2, roi_h, roi_w))

            cls_score = F.max(roi_score, axis=2)
            assert cls_score.shape == (1, self.C+1, roi_h, roi_w)
            cls_score = F.sum(cls_score, axis=(2, 3))
            cls_score /= (roi_h * roi_w)
            cls_scores[i_roi] = cuda.to_cpu(cls_score.data)[0]
            assert cls_score.shape == (1, self.C+1)

            a_loss_cls = F.softmax_cross_entropy(cls_score, roi_cls_var)
            loss_cls += a_loss_cls
            n_loss_cls += 1

            roi_cls_pred = F.argmax(cls_score, axis=1)
            roi_cls_pred = int(roi_cls_pred.data[0])
            roi_clss_pred[i_roi] = roi_cls_pred

            roi_score_io = roi_score[:, roi_cls, :, :, :]
            assert roi_score_io.shape == (1, 2, roi_h, roi_w)

            if roi_cls != 0:
                roi_seg = roi_seg.astype(np.int32)
                roi_seg = utils.resize_image(roi_seg, (roi_h, roi_w))
                roi_seg = roi_seg[np.newaxis, :, :]
                if xp == cupy:
                    roi_seg = cuda.to_gpu(roi_seg, device=x.data.device)
                roi_seg = Variable(roi_seg, volatile='auto')
                a_loss_seg = F.softmax_cross_entropy(roi_score_io, roi_seg)
                loss_seg += a_loss_seg
                n_loss_seg += 1

            roi_score_io = cuda.to_cpu(roi_score_io.data)[0]
            roi_seg_pred = np.argmax(roi_score_io, axis=0)
            roi_seg_pred = roi_seg_pred.astype(bool)

            roi_mask_prob = F.softmax(roi_score[0])[:, 1, :, :]
            roi_mask_probs[i_roi] = cuda.to_cpu(roi_mask_prob.data)

        if n_loss_cls != 0:
            loss_cls /= n_loss_cls
        if n_loss_seg != 0:
            loss_seg /= n_loss_seg
        loss = loss_rpn + loss_cls + loss_seg

        self.loss_cls = float(loss_cls.data)
        self.loss_seg = float(loss_seg.data)
        self.loss_rpn = float(loss_rpn.data)
        self.loss = float(loss.data)

        # rois -> label
        lbl_ins_pred, lbl_cls_pred = utils.roi_scores_to_label(
            (x.shape[2], x.shape[3]), rois, cls_scores, roi_mask_probs,
            down_scale, self.k, self.C)

        self.roi_clss = roi_clss
        self.roi_clss_pred = roi_clss_pred
        self.lbl_cls_pred = lbl_cls_pred
        self.lbl_ins_pred = lbl_ins_pred

        self.accuracy_cls = sklearn.metrics.accuracy_score(
            roi_clss, roi_clss_pred)
        self.iu_lbl_cls = fcn.utils.label_accuracy_score(
            lbl_cls, self.lbl_cls_pred, self.C+1)[2]
        self.iu_lbl_ins = utils.instance_label_accuracy_score(
            lbl_ins, self.lbl_ins_pred)

        return loss
