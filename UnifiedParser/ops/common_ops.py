# Copyright © 2022 Arm Technology (China) Co. Ltd. All rights reserved.
# SPDX-License-Identifier: Apache-2.0


import tensorflow.compat.v1 as tf
import numpy as np
from .op import *
from ..common.utils import get_random_array, list_list_to_string
from ..plugin_loader import PARSER_OP_DICT


class AccidentalHitsOp(OpHasMultipleOutPorts, CommonOp):
    @classmethod
    def attributes(cls):
        return {'num_true': {'type': AttrType.INT, 'required': True},
                }

    def __init__(self, graph, attr_dict=None):
        super(AccidentalHitsOp, self).__init__(graph, attr_dict)
        self.update_attributes(AccidentalHitsOp, attr_dict)
        assert self.check_required(), 'AccidentalHitsOp is missing a required parameter.'

    def infer_shape(self):
        super(AccidentalHitsOp, self).infer_shape()
        input_shape = self.get_input_shapes()[0]
        num_hits = min(np.prod(input_shape), 32768)
        output_indices = get_random_array([num_hits], 'int32')
        output_ids = get_random_array([num_hits], 'int32')
        output_effective_len = get_random_array([1], 'int32')
        self.set_out_tensor([output_indices, output_ids, output_effective_len])


class BatchGatherOp(OpHasAxis, OpHasOneOutPort, CommonOp):
    @classmethod
    def attributes(cls):
        return {'axis': {'default': 0},
                'batch_dims': {'type': AttrType.INT, 'default': 0}}

    def __init__(self, graph, attr_dict=None):
        super(BatchGatherOp, self).__init__(graph, attr_dict)
        self.update_attributes(BatchGatherOp, attr_dict)
        assert self.check_required(), 'BatchGatherOp is missing a required parameter.'

    def infer_shape(self):
        super(BatchGatherOp, self).infer_shape()
        inputs = self.get_input_tensors()
        indices = inputs[1].tolist()
        ref_shape = inputs[0].shape
        indices = np.array(indices, np.int64)
        negative_axes = indices < 0
        if np.any(negative_axes):
            len_shape = ref_shape[self.axis]
            indices[negative_axes] += len_shape
        indices = indices.tolist()
        out_tensor = tf.gather(inputs[0],
                               indices,
                               axis=self.axis,
                               batch_dims=self.batch_dims).eval()
        self.set_out_tensor(out_tensor)


class BNLLOp(OpHasOneOutPort, CommonOp):
    def infer_shape(self):
        super(BNLLOp, self).infer_shape()
        inputs = self.get_input_tensors()
        out_tensor = np.log(1. + np.exp(*inputs))
        self.set_out_tensor(out_tensor)


class CropAndResizeOp(OpHasMethod, LayoutConcernedOp, OpHasOneOutPort, CommonOp):
    @classmethod
    def attributes(cls):
        return {'crop_size': {'type': AttrType.INTS, 'default': []},
                'method': {'default': 'BILINEAR', 'options': ['BILINEAR', 'NEAREST']},
                'extrapolation_value': {'type': AttrType.FLOAT, 'default': 0.},
                }

    def __init__(self, graph, attr_dict=None):
        super(CropAndResizeOp, self).__init__(graph, attr_dict)
        self.update_attributes(CropAndResizeOp, attr_dict)
        assert self.check_required(), 'CropAndResizeOp is missing a required parameter.'

    def infer_shape(self):
        super(CropAndResizeOp, self).infer_shape()
        inputs = self.get_input_tensors()
        out_tensor = tf.image.crop_and_resize(image=inputs[0],
                                              boxes=inputs[1],
                                              box_indices=inputs[2],
                                              crop_size=self.crop_size,
                                              method=self.method.lower(),
                                              extrapolation_value=self.extrapolation_value).eval()
        self.set_out_tensor(out_tensor)


class ChannelShuffleOp(LayoutConcernedOp, OpHasMultipleOutPorts, CommonOp):
    @classmethod
    def attributes(cls):
        return {'group': {'type': AttrType.INT, 'required': True, 'default': 2},
                'splits': {'type': AttrType.INT, 'required': True, 'default': 1}
                }

    def __init__(self, graph, attr_dict=None):
        super(ChannelShuffleOp, self).__init__(graph, attr_dict)
        self.update_attributes(ChannelShuffleOp, attr_dict)
        assert self.check_required(), 'ChannelShuffleOp is missing a required parameter.'

    def infer_shape(self):
        super(ChannelShuffleOp, self).infer_shape()
        inputs = self.get_input_tensors()
        inp = np.transpose(inputs[0], (0, 3, 1, 2)
                           ) if self.data_format == 'NHWC' else inputs[0]
        out_tensor = torch.nn.functional.channel_shuffle(
            torch.from_numpy(inp), self.group).numpy()
        if self.data_format == 'NHWC':
            out_tensor = np.transpose(out_tensor, (0, 2, 3, 1))
            out_tensors = np.split(out_tensor, self.splits, axis=-1)
        else:
            out_tensors = np.split(out_tensor, self.splits, axis=1)
        self.set_out_tensor(out_tensors)


class CTCGreedyDecoderOp(OpHasOneOutPort, CommonOp):
    @classmethod
    def attributes(cls):
        return {'merge_repeated': {'type': AttrType.INT, 'default': 1},
                'sequence_lens': {'type': AttrType.INT},
                'input_size': {'type': AttrType.INT}
                }

    def __init__(self, graph, attr_dict=None):
        super(CTCGreedyDecoderOp, self).__init__(graph, attr_dict)
        self.update_attributes(CTCGreedyDecoderOp, attr_dict)
        assert self.check_required(), 'CTCGreedyDecoderOp is missing a required parameter.'

    def infer_shape(self):
        super(CTCGreedyDecoderOp, self).infer_shape()
        inputs = self.get_input_tensors()
        if self.data_format == 'NHWC':
            batch_size, self.sequence_lens, self.input_size = inputs[0].shape
        else:
            self.sequence_lens, batch_size, self.input_size = inputs[0].shape
        out_tensor = get_random_array([batch_size, 4096, 1, 1], 'int64')
        self.set_out_tensor(out_tensor)


class DummyOp(OpHasOneOutPort, ConstLikeOp, CommonOp):
    def __init__(self, graph, attr_dict=None):
        super(DummyOp, self).__init__(graph, attr_dict)

    def infer_shape(self):
        super(DummyOp, self).infer_shape()


class FillOp(OpHasOneOutPort, CommonOp):
    def infer_shape(self):
        super(FillOp, self).infer_shape()
        inputs = self.get_input_tensors()
        out_tensor = tf.fill(*inputs).eval()
        self.set_out_tensor(out_tensor)


class FilterOp(OpHasMultipleOutPorts, CommonOp):
    def infer_shape(self):
        super(FilterOp, self).infer_shape()
        inputs = self.get_input_tensors()
        mask = inputs[-1].astype(np.bool)
        out_tensors = [np.zeros_like(inp) for inp in inputs[:-1]]
        for i, ot in enumerate(inputs[:-1]):
            true_indices = np.where(mask)[0]
            out_tensors[i][mask] = np.take(ot, true_indices, axis=0)
        valid_num = np.array([np.sum(mask)], np.int32)
        out_tensors.append(valid_num)
        self.set_out_tensor(out_tensors)


class FullyConnectedOp(BaseLinearOp, CommonOp):
    @classmethod
    def attributes(cls):
        return {}

    @classmethod
    def perm_onnx_to_tf(cls):
        return [1, 0]

    def __init__(self, graph, attr_dict=None):
        super(FullyConnectedOp, self).__init__(graph, attr_dict)
        self.update_attributes(FullyConnectedOp, attr_dict)
        assert self.check_required(), 'FullyConnectedOp is missing a required parameter.'

    def infer_shape(self):
        super(FullyConnectedOp, self).infer_shape()
        inputs = self.get_input_tensors()
        out_tensor = (tf.matmul(inputs[0], np.transpose(self.weights, axes=type(self).perm_onnx_to_tf()))
                      + self.biases
                      ).eval()
        self.set_out_tensor(out_tensor)


class GeluOp(BaseActivationOp, CommonOp):
    @classmethod
    def attributes(cls):
        return {'approximate': {'type': AttrType.STRINGS, 'default': 'none', 'options': ['none', 'tanh']},
                }

    def __init__(self, graph, attr_dict=None):
        super(GeluOp, self).__init__(graph, attr_dict)
        self.update_attributes(GeluOp, attr_dict)
        assert self.check_required(), 'GeluOp is missing a required parameter.'
        self.activations = 'GELU'

    def infer_shape(self):
        super(GeluOp, self).infer_shape()
        inputs = self.get_input_tensors()
        if self.approximate == 'tanh':
            out = 0.5*(inputs[0])*(1.0+tf.math.tanh(inputs[0]
                                                    * 0.7978845608*(1.0+0.044715*inputs[0]*inputs[0])))
            out_tensor = out.eval().astype(np.float32)
        else:
            out_tensor = 0.5 * \
                (inputs[0])*(1.0+(inputs[0]*0.7978845608 *
                                  (1.0+0.044715*inputs[0]*inputs[0])))
        self.set_out_tensor(out_tensor)


class GenerateProposalsOp(LayoutConcernedOp, OpHasWeights, OpHasMultipleOutPorts, CommonOp):
    @classmethod
    def attributes(cls):
        return {'pre_nms_topn': {'type': AttrType.INT, 'default': 6000},
                'post_nms_topn': {'type': AttrType.INT, 'default': 300},
                'min_size': {'type': AttrType.INT, 'default': 16},
                'nms_threshold': {'type': AttrType.FLOAT, 'default': 0.7},
                'feature_stride': {'type': AttrType.INT, 'default': 16},
                'image_width': {'type': AttrType.INT, 'default': 600},
                'image_height': {'type': AttrType.INT, 'default': 600},
                }

    def __init__(self, graph, attr_dict=None):
        super(GenerateProposalsOp, self).__init__(graph, attr_dict)
        self.update_attributes(GenerateProposalsOp, attr_dict)
        assert self.check_required(), 'GenerateProposalsOp is missing a required parameter.'

    def infer_shape(self):
        super(GenerateProposalsOp, self).infer_shape()
        inputs = self.get_input_tensors()
        batch_size = inputs[0].shape[0]
        scores = np.random.ranf(
            (batch_size, self.post_nms_topn)).astype(np.float32)
        boxes = np.random.ranf(
            (batch_size, self.post_nms_topn, 4)).astype(np.float32)
        indices = np.random.ranf(
            (batch_size, self.post_nms_topn, 1)).astype(np.float32)
        box_num = np.random.ranf((batch_size, 1)).astype(np.float32)
        self.set_out_tensor([scores, boxes, indices, box_num])


class HardSwishOp(LayoutUnawareOp, OpHasOneOutPort, CommonOp):
    @classmethod
    def attributes(cls):
        return {}

    def __init__(self, graph, attr_dict=None):
        super(HardSwishOp, self).__init__(graph, attr_dict)
        self.update_attributes(HardSwishOp, attr_dict)
        assert self.check_required(), 'HardSwishOp is missing a required parameter.'

    def infer_shape(self):
        super(HardSwishOp, self).infer_shape()
        inputs = self.get_input_tensors()
        out_tensor = (inputs[0] * tf.nn.relu6(inputs[0] + 3) / 6).eval()
        self.set_out_tensor(out_tensor)


class InputOp(OpHasOneOutPort, InputLikeOp, CommonOp):
    def infer_shape(self, input_tensor=None):
        super(InputOp, self).infer_shape()
        assert input_tensor is not None, 'input shape is empty in InputOp.'
        out_tensor = input_tensor.copy()
        self.set_out_tensor(out_tensor)


class InTopKOp(LayoutUnawareOp, OpHasOneOutPort, CommonOp):
    @classmethod
    def attributes(cls):
        return {'k': {'type': AttrType.INT, 'required': True}}

    def __init__(self, graph, attr_dict=None):
        super(InTopKOp, self).__init__(graph, attr_dict)
        self.update_attributes(InTopKOp, attr_dict)
        assert self.check_required(), 'InTopKOp is missing a required parameter.'

    def infer_shape(self):
        super(InTopKOp, self).infer_shape()
        inputs = self.get_input_tensors()
        assert len(
            inputs) == 2, 'InTopKOp expects two inputs, but got %d.' % len(inputs)
        out_tensor = tf.raw_ops.InTopK(
            predictions=inputs[0], targets=inputs[1], k=self.k).eval()
        self.set_out_tensor(out_tensor)


class LayerNormOp(OpHasAxis, OpHasBiases, OpHasWeights, OpHasOneOutPort, CommonOp):
    @classmethod
    def attributes(cls):
        return {'epsilon': {'type': AttrType.FLOAT, 'required': True, 'default': 1e-5}}

    def __init__(self, graph, attr_dict=None):
        super(LayerNormOp, self).__init__(graph, attr_dict)
        self.update_attributes(LayerNormOp, attr_dict)
        assert self.check_required(), 'LayerNormOp is missing a required parameter.'

    def infer_shape(self):
        super(LayerNormOp, self).infer_shape()
        inputs = self.get_input_tensors()
        mean = np.mean(inputs[0], axis=tuple(self.axes), keepdims=True)
        variance = np.var(inputs[0], axis=tuple(self.axes), keepdims=True)
        ngamma = 1.0 / ((variance + self.epsilon) ** 0.5)
        out_tensor = (inputs[0] - mean) * ngamma
        axes = OpHasAxis.make_axes_non_negative(
            self.axes, len(inputs[0].shape))
        weights = OpHasAxis.expand_to(self.weights, axes, len(inputs[0].shape))
        biases = OpHasAxis.expand_to(self.biases, axes, len(inputs[0].shape))
        out_tensor = out_tensor * weights + biases
        self.set_out_tensor(out_tensor)


class MeshgridOp(OpHasMultipleOutPorts, CommonOp):
    @classmethod
    def attributes(cls):
        return {'indexing': {'type': AttrType.STRING, 'options': ['ij', 'xy'], 'default': 'xy'}}

    def __init__(self, graph, attr_dict=None):
        super(MeshgridOp, self).__init__(graph, attr_dict)
        self.update_attributes(MeshgridOp, attr_dict)
        assert self.check_required(), 'MeshgridOp is missing a required parameter.'

    def infer_shape(self):
        super(MeshgridOp, self).infer_shape()
        inputs = self.get_input_tensors()
        out_tensors = tf.meshgrid(*inputs, indexing=self.indexing)
        out_tensors = [t.eval() for t in out_tensors]
        self.set_out_tensor(out_tensors)


class MishOp(LayoutUnawareOp, OpHasOneOutPort, CommonOp):
    def infer_shape(self):
        super(MishOp, self).infer_shape()
        inputs = self.get_input_tensors()
        out_tensor = inputs[0] * np.tanh(np.log(np.exp(inputs[0]) + 1))
        self.set_out_tensor(out_tensor)


class MomentsOp(OpHasMultipleOutPorts, OpHasAxis, CommonOp):
    def infer_shape(self):
        super(MomentsOp, self).infer_shape()
        inputs = self.get_input_tensors()
        out_tensors = tf.nn.moments(
            inputs[0], self.axes, keepdims=self.keepdims)
        out_tensors = [out_tensor.eval() for out_tensor in out_tensors]
        self.set_out_tensor(out_tensors)


class OutOp(OpHasOneOutPort, CommonOp):
    def __init__(self, graph, attr_dict=None):
        super(OutOp, self).__init__(graph, attr_dict)

    def infer_shape(self):
        super(OutOp, self).infer_shape()


class PluginOp(OpHasVariableOutPorts, CommonOp):
    @classmethod
    def num_in_ports(cls):
        return 1

    def __init__(self, graph, attr_dict=None):
        super(PluginOp, self).__init__(graph, attr_dict)
        self._type = 'Plugin' + attr_dict.get('type', 'Plugin')
        fw = graph._attr['framework'].name
        try:
            plugin_type = PARSER_OP_DICT[re.sub(
                r'^Plugin', '', self._type, count=1)]
            nest_input = None
            if "_nest_inputs" in attr_dict:
                nest_input = attr_dict["_nest_inputs"]
                attr_dict.pop("_nest_inputs")
            self._plugin = plugin_type(fw, attr_dict)
            if nest_input is not None:
                self._plugin._nest_inputs = nest_input
        except Exception as e:
            self._plugin = None
            WARN('[Parser]: Creating plugin type (%s) meets error %s! ' %
                 (self.type, str(e)))
            import traceback
            print(traceback.format_exc())

    def infer_shape(self):
        super(PluginOp, self).infer_shape()
        inputs = self.get_input_tensors()
        out_tensors = []
        if self._plugin is not None:
            # check if is subgraph plugin, use nest list
            if hasattr(self._plugin, "_subgraph_type"):
                nest_input_index = getattr(self._plugin, "_nest_inputs", [])
                inputs = [[inputs[i] for i in inps]
                          for inps in nest_input_index]
            try:
                out_tensors = self._plugin.infer_shape(inputs)
            except Exception as e:
                WARN('[Parser]: plugin type (%s) infer shape meets error %s! ' %
                     (self.type, str(e)))
                import traceback
                print(traceback.format_exc())
        else:
            WARN('[Parser]: Invalid Plugin op (%s) for infer_shape!' % self.name)
        self.set_out_tensor(out_tensors)

    def nchw_to_nhwc(self):
        if self._plugin is not None:
            self._plugin.nchw_to_nhwc()
        else:
            WARN('[Parser]: Invalid Plugin op (%s) for nchw_to_nhwc' % self.name)

    def write_attrs(self, txt_file):
        ret = super(PluginOp, self).write_attrs(txt_file)
        if ret:
            if self._plugin is not None:
                for k, v in getattr(self._plugin, 'params', {}).items():
                    if isinstance(v, np.ndarray):
                        v = v.tolist()
                    if isinstance(v, (list,)):
                        try:
                            v = str(v).replace(' ', '')
                        except Exception as e:
                            WARN('[Parser]: Node(%s) meets error for write_attrs: %s' %
                                 (self.name, str(e)))
                            continue
                    elif isinstance(v, (int, float)):
                        v = str(v)
                    elif isinstance(v, str):
                        pass
                    else:
                        try:
                            v = str(v)
                        except Exception as e:
                            WARN('[Parser]: Node(%s) meets error for write_attrs: %s' %
                                 (self.name, str(e)))
                            continue
                    txt_file.write('%s=%s\n' % (k, v))
            else:
                WARN('[Parser]: Invalid Plugin op(%s) for write_attrs!' % self.name)
                ret = False
        return ret


class ReduceAllOp(OpHasAxis, OpHasOneOutPort, CommonOp):
    @classmethod
    def attributes(cls):
        return {'keepdims': {'required': True}}

    def __init__(self, graph, attr_dict=None):
        super(ReduceAllOp, self).__init__(graph, attr_dict)
        self.update_attributes(ReduceAllOp, attr_dict)
        assert self.check_required(), 'ReduceAllOp is missing a required parameter.'

    def infer_shape(self):
        super(ReduceAllOp, self).infer_shape()
        inputs = self.get_input_tensors()
        if self.axes is None:
            self.axes = list(range(len(inputs[0].shape)))
        out_tensor = tf.reduce_all(inputs[0], axis=tuple(
            self.axes), keepdims=self.keepdims).eval()
        self.set_out_tensor(out_tensor)


class ReduceAnyOp(OpHasAxis, OpHasOneOutPort, CommonOp):
    @classmethod
    def attributes(cls):
        return {'keepdims': {'default': 1}}

    def __init__(self, graph, attr_dict=None):
        super(ReduceAnyOp, self).__init__(graph, attr_dict)
        self.update_attributes(ReduceAnyOp, attr_dict)
        assert self.check_required(), 'ReduceAnyOp is missing a required parameter.'

    def infer_shape(self):
        super(ReduceAnyOp, self).infer_shape()
        inputs = self.get_input_tensors()
        if self.axes is None:
            self.axes = list(range(len(inputs[0].shape)))
        out_tensor = tf.reduce_any(inputs[0], axis=tuple(
            self.axes), keepdims=self.keepdims).eval()
        self.set_out_tensor(out_tensor)


class ReduceVarianceOp(OpHasAxis, OpHasOneOutPort, OnnxOp):
    @classmethod
    def attributes(cls):
        return {1: {'keepdims': {'default': 1},
                    'unbiased': {'type': AttrType.INT, 'default': 0, 'options': [0, 1]}}
                }

    def __init__(self, graph, attr_dict=None):
        super(ReduceVarianceOp, self).__init__(graph, attr_dict)
        self.update_attributes(ReduceVarianceOp, attr_dict)
        assert self.check_required(), 'ReduceVarianceOp is missing a required parameter.'

    def __getattr__(self, item):
        ret = None
        try:
            if item in ('keepdims', 'unbiased'):
                ret = bool(self.__dict__['_attr'][item].value)
        except:
            ret = None
        if ret is None:
            ret = super(ReduceVarianceOp, self).__getattr__(item)
        return ret

    def infer_shape(self):
        super(ReduceVarianceOp, self).infer_shape()
        inputs = self.get_input_tensors()
        if self.axes is None:
            self.axes = list(range(len(inputs[0].shape)))
        out_tensor = np.var(
            inputs[0], axis=tuple(self.axes), keepdims=self.keepdims, ddof=self.unbiased)
        self.set_out_tensor(out_tensor)


class RollOp(OpHasOneOutPort, CommonOp):
    def __init__(self, graph, attr_dict=None):
        super(RollOp, self).__init__(graph, attr_dict)

    def infer_shape(self):
        super(RollOp, self).infer_shape()
        inputs = self.get_input_tensors()
        out_tensor = tf.roll(*inputs).eval()
        self.set_out_tensor(out_tensor)


class SegmentReduceOp(OpHasMethod, OpHasOneOutPort, CommonOp):
    FUNC_MAP = {'SUM': tf.math.segment_sum,
                'PROD': tf.math.segment_prod,
                'MIN': tf.math.segment_min,
                'MAX': tf.math.segment_max,
                'MEAN': tf.math.segment_mean,
                }

    @classmethod
    def attributes(cls):
        return {'method': {'options': ['SUM', 'PROD', 'MIN', 'MAX', 'MEAN'], 'default': 'SUM'}}

    def __init__(self, graph, attr_dict=None):
        super(SegmentReduceOp, self).__init__(graph, attr_dict)
        self.update_attributes(SegmentReduceOp, attr_dict)
        assert self.check_required(), 'SegmentReduceOp is missing a required parameter.'

    def infer_shape(self):
        super(SegmentReduceOp, self).infer_shape()
        inputs = self.get_input_tensors()
        out_tensor = SegmentReduceOp.FUNC_MAP[self.method](*inputs).eval()
        self.set_out_tensor(out_tensor)


class SiluOp(OpHasOneOutPort, CommonOp):
    def __init__(self, graph, attr_dict=None):
        super(SiluOp, self).__init__(graph, attr_dict)
        self.update_attributes(SiluOp, attr_dict)
        assert self.check_required(), 'SiluOp is missing a required parameter.'

    def infer_shape(self):
        super(SiluOp, self).infer_shape()
        inputs = self.get_input_tensors()
        tor_arr = torch.from_numpy(inputs[0])
        m = torch.nn.SiLU()
        out_tensor = m(tor_arr)
        tor2numpy = out_tensor.numpy()
        self.set_out_tensor(tor2numpy)


class UndefinedOp(OpHasVariableOutPorts, CommonOp):
    def __init__(self, graph, attr_dict=None):
        super(UndefinedOp, self).__init__(graph, attr_dict)
        if 'type' in attr_dict:
            self.type = attr_dict['type']
        for k, v in attr_dict.items():
            if k not in ('name', 'type', 'data_format') and k not in self._attr.keys():
                attr_param = {'type': AttrType.UNDEFINED,
                              'dafault': None, 'required': False, 'value': v}
                self._attr[k] = Attribute(k, attr_param)

    def infer_shape(self, input_tensor=None):
        super(UndefinedOp, self).infer_shape()

    def write_attrs(self, txt_file):
        ret = super(UndefinedOp, self).write_attrs(txt_file)
        if ret:
            for k, v in self._attr.items():
                if v.type == AttrType.UNDEFINED:
                    txt_file.write('%s=%s\n' % (k, str(v.value)))
        return ret


class ZeroFractionOp(OpHasOneOutPort, CommonOp):
    def infer_shape(self):
        super(ZeroFractionOp, self).infer_shape()
        input_tensor = self.get_input_tensors()[0]
        out_tensor = np.array(tf.math.zero_fraction(input_tensor).eval())
        self.set_out_tensor(out_tensor)
