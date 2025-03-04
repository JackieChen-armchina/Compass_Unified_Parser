# Copyright © 2022 Arm Technology (China) Co. Ltd. All rights reserved.
# SPDX-License-Identifier: Apache-2.0


import abc
import inspect
import re
import copy
from functools import reduce
from collections import OrderedDict, defaultdict
from collections.abc import Iterable
import numpy as np
import torch
from ..common.defs import TensorType, Tensor, AttrType, Attribute, Framework, FLOAT_EQUAL
from ..logger import INFO, DEBUG, WARN, ERROR, FATAL
from ..common.utils import num_list_to_string, string_list_to_string, extend_lists


class Op(abc.ABC):
    '''
    OP's base class, which all OP's concrete classes must inherit from it.
    Attributes:
        graph: Describe the graph structure of the entire model.
        type: Op type of the node.
        attr: Attributes for this Op.
        update_attributes: A method to update the attributes of this Op.
    '''
    @staticmethod
    def framework_to_op(fw):
        ''' Returns the framework type of the op. '''
        assert isinstance(fw, Framework), 'Fw is not a valid framework type.'
        ret = []
        if fw == Framework.ONNX:
            ret = [OnnxOp, CommonOp]
        elif fw == Framework.TFLITE:
            ret = [TfliteOp, OnnxOp, CommonOp]
        elif fw == Framework.CAFFE:
            ret = [CaffeOp, OnnxOp, CommonOp]
        elif fw == Framework.TENSORFLOW:
            ret = [TfOp, OnnxOp, CommonOp]
        else:
            FATAL('[Parser]: Unsupported Framework %s!' % fw.name)
        return ret

    @staticmethod
    def cal_inverse_perm(src_perm):
        '''Calculate perm after inverse.'''
        if src_perm is None:
            return None
        elif isinstance(src_perm, (list, tuple)):
            if len(src_perm) == 0:
                return None
            else:
                return [src_perm.index(i) for i in range(len(src_perm))]

    @staticmethod
    def shape_nchw_to_nhwc(shape):
        '''Calculate the shape of the tensor after converting nchw to nhwc.'''
        assert len(
            shape) == 4, 'The length of shape is invalid in Op shape_nchw_to_nhwc.'
        in_shape = np.array(list(shape), np.int64)
        index = np.array([0, 2, 3, 1], np.int64)
        out_shape = in_shape[index].tolist()
        return out_shape

    @staticmethod
    def shape_nhwc_to_nchw(shape):
        '''Calculate the shape of the tensor after converting nhwc to nchw.'''
        assert len(
            shape) == 4, 'The length of shape is invalid in Op shape_nchw_to_nhwc.'
        in_shape = np.array(list(shape), np.int64)
        index = np.array([0, 3, 1, 2], np.int64)
        out_shape = in_shape[index].tolist()
        return out_shape

    @classmethod
    def perm_nchw_to_nhwc(cls):
        '''Calculate the perm required to convert nchw to nhwc.'''
        return [0, 2, 3, 1]

    @classmethod
    def perm_nhwc_to_nchw(cls):
        '''Calculate the perm required to convert nhwc to nchw.'''
        return Op.cal_inverse_perm(cls.perm_nchw_to_nhwc())

    @classmethod
    def perm_ncw_to_nwc(cls):
        '''Calculate the perm required to convert ncw to nwc.'''
        return [0, 2, 1]

    @classmethod
    def perm_nwc_to_ncw(cls):
        '''Calculate the perm required to convert nwc to ncw.'''
        return Op.cal_inverse_perm(cls.perm_ncw_to_nwc())

    @classmethod
    def default_onnx_version(cls):
        '''get default onnx version.'''
        return 1

    @classmethod
    def attributes(cls):
        '''return attributes of OP class.'''
        return {'name': {'type': AttrType.STRING, 'default': '', 'required': True},
                'data_format': {'type': AttrType.STRING,
                                'default': 'NHWC',
                                'options': ['NWC', 'NCW', 'NHWC', 'NCHW', 'NDHWC', 'NCDHW'],
                                'required': True},
                'cur_version': {'type': AttrType.INT, 'default': 0, 'required': False},
                }

    @classmethod
    def get_concrete_subclass_names(cls):
        '''Get concrete subclass names of OP class.'''
        def _get_subclass_names(class_type):
            all_subclasses = []
            for subclass in class_type.__subclasses__():
                all_subclasses.append(subclass)
                all_subclasses.extend(_get_subclass_names(subclass))
            return all_subclasses
        ret = [re.sub(r'Op$', '', class_type.__name__)
               for class_type in _get_subclass_names(cls)
               if not inspect.isabstract(class_type)
               ]
        return list(set(ret))

    @classmethod
    def get_parent_class_types(cls):
        '''Get parent class types of OP class.'''
        return [t for t in inspect.getmro(cls) if re.search(r'^Op|Op$', t.__name__)]

    @classmethod
    def cast_in_ports(cls):
        '''Returns the port index to which the cast needs to be added and the cast dtype to be converted.'''
        return {}

    def __init__(self, graph, attr_dict=None):
        '''Inits SampleClass.'''
        self._graph = graph
        self._type = re.sub(r'Op$', '', type(self).__name__, count=1)
        self._attr = {}
        self.update_attributes(Op, attr_dict)

    def __getattr__(self, item):
        '''Returns the OP object property value.'''
        if item in ('_graph', '_type', '_attr', 'type'):
            return self.__dict__.get(item if item.startswith('_') else '_' + item, None)
        elif item in self.__dict__['_attr'] and not isinstance(self.__dict__['_attr'][item], Attribute):
            return self.__dict__['_attr'][item]
        else:
            ret = None
            if self.__dict__['_attr'].get(item, None) is not None:
                ret = self.__dict__['_attr'][item].value
            elif self.cur_version in type(self).attributes() \
                    and 'default' in type(self).attributes()[self.cur_version]:
                ret = type(self).attributes()[self.cur_version]['default']
            else:
                raise AttributeError('[Parser]: %r object has no attribute %r' % (
                    self.__class__.__name__, item))
            return ret

    def __setattr__(self, key, value):
        '''Used to set the attribute value of the OP object, the attribute does not necessarily exist.'''
        if key in ('_graph', '_type', '_attr', 'type'):
            if key in ('_type', 'type'):
                self.__dict__.update({'_type': str(value)})
            else:
                self.__dict__.update({key: value})
        else:
            try:
                value_type = self.__dict__['_attr'][key].type
                if value_type == AttrType.STRING:
                    value = str(value)
                elif value_type == AttrType.INT:
                    value = int(value)
                elif value_type == AttrType.INTS:
                    if not isinstance(value, Iterable):
                        value = [int(value)]
                    else:
                        value = [int(v) for v in list(value)]
                elif value_type == AttrType.FLOAT:
                    value = float(value)
                elif value_type == AttrType.FLOATS:
                    if not isinstance(value, Iterable):
                        value = [float(value)]
                    else:
                        value = [float(v) for v in list(value)]
                self.__dict__['_attr'][key].value = value
            except:
                self.__dict__['_attr'][key] = value

    def update_attributes(self, cls, attr_dict):
        '''update attributes of OP object.'''
        supported_ops = (OnnxOp, TfliteOp, CaffeOp, TfOp)
        if inspect.isabstract(cls) or not isinstance(self, supported_ops):
            for attr_key, attr_v in cls.attributes().items():
                attr_param = copy.deepcopy(attr_v)
                if attr_key in attr_dict:
                    attr_param.update(
                        {'value': copy.deepcopy(attr_dict[attr_key])})
                elif 'default' in attr_param:
                    attr_param.update(
                        {'value': copy.deepcopy(attr_param['default'])})
                if attr_key in self._attr:
                    self._attr[attr_key].update(attr_param)
                else:
                    self._attr[attr_key] = Attribute(attr_key, attr_param)
        else:
            if isinstance(self, supported_ops):
                cls_attrs = copy.deepcopy(cls.attributes())
                if isinstance(self, (TfliteOp, TfOp)):
                    self._attr['cur_version'].value = self._attr['opcode_version'].value
                    if self._attr['cur_version'].value not in cls.attributes().keys():
                        keys = sorted(list(cls.attributes().keys()))
                        updates = copy.deepcopy(
                            cls_attrs[keys[-1]]) if keys else {}
                        cls_attrs.update(
                            {self._attr['cur_version'].value: updates})
                elif isinstance(self, (CaffeOp, )):
                    self._attr['cur_version'].value = 1
                assert 'cur_version' in self._attr and self._attr['cur_version'].value in cls_attrs.keys(), \
                    ('[Parser]: Please check attributes of %s Node (%s)!' %
                     (self.type, self.name))

                for k, v in cls_attrs[self._attr['cur_version'].value].items():
                    attr_param = copy.deepcopy(v)
                    if k in attr_dict:
                        try:
                            attr_param.update(
                                {'value': copy.deepcopy(attr_dict[k])})
                        except:
                            attr_param.update({'value': attr_dict[k]})
                    elif 'default' in attr_param:
                        attr_param.update(
                            {'value': copy.deepcopy(attr_param['default'])})
                    if k in self._attr:
                        self._attr[k].update(attr_param)
                    else:
                        self._attr[k] = Attribute(k, attr_param)
            else:
                WARN('[Parser]: Node(%s) attributes are not updated, because there is an unsupported OP!' %
                     (self.name))

    def copied_attr(self):
        '''Returns the copied attr of this Op.'''
        return {k: copy.deepcopy(self._attr[k].value if isinstance(self._attr[k], Attribute) else self._attr[k]) for k in self._attr.keys()}

    def check_required(self):
        '''Check if the required attr is available.'''
        if inspect.isabstract(type(self)):
            return True
        for attr_key, v in self._attr.items():
            if isinstance(v, Attribute):
                if getattr(v, 'required', False) is True and getattr(v, 'value', None) is None:
                    WARN('[Parser]: Required fields [%s] not exists!' % attr_key)
                    return False
                if getattr(v, 'options', []) and getattr(v, 'value', None) not in getattr(v, 'options'):
                    WARN('[Parser]: Value [%s] not in options of [%s]!' %
                         (str(getattr(v, 'value', None)), attr_key))
                    return False
        return True

    @abc.abstractmethod
    def infer_shape(self):
        '''An abstract method for shape inference.'''
        pass

    def write_attrs(self, txt_file):
        '''Write the required attr in IR.'''
        ret = True
        if not txt_file.closed and txt_file.mode == 'w':
            bottom_info, top_info = self.get_inputs_info(), self.get_outputs_info()
            if self.name in self._graph._attr['duplicate_name']:
                newname = self._graph._attr['duplicate_name'][self.name]
                txt_file.write('layer_name=%s\n' % newname)
            else:
                txt_file.write('layer_name=%s\n' % self.name)
            if re.search(r'^Plugin', self.type):
                if hasattr(self, "_plugin") and self._plugin is not None:
                    txt_file.write('layer_type=%s\n' % self._plugin.op_type)
                else:
                    txt_file.write('layer_type=%s\n' %
                                   re.sub(r'^Plugin', '', self.type))
            else:
                txt_file.write('layer_type=%s\n' %
                               re.sub(r'^Arm', '', self.type))
            txt_file.write('layer_bottom=[%s]\n' % string_list_to_string(
                bottom_info[0] if len(bottom_info) == 3 else []))
            txt_file.write('layer_bottom_shape=[%s]\n' % string_list_to_string(
                bottom_info[1] if len(bottom_info) == 3 else []))
            txt_file.write('layer_bottom_type=[%s]\n' % string_list_to_string(
                bottom_info[2] if len(bottom_info) == 3 else []))
            txt_file.write('layer_top=[%s]\n' % string_list_to_string(
                top_info[0] if len(top_info) >= 3 else []))
            txt_file.write('layer_top_shape=[%s]\n' % string_list_to_string(
                top_info[1] if len(top_info) >= 3 else []))
            txt_file.write('layer_top_type=[%s]\n' % string_list_to_string(
                top_info[2] if len(top_info) >= 3 else []))
            range_str = string_list_to_string(
                top_info[3] if len(top_info) >= 4 else [])
            if range_str:
                txt_file.write('layer_top_range=[%s]\n' % range_str)
        else:
            FATAL(
                '[Parser]: Invalid file to write properties for Node(%s) in write_attrs!' % (self.name))
        return ret

    def get_input_tensors(self):
        '''Get input tensor of the node.'''
        try:
            return [d['tensor'].value if d['tensor'] is not None else None
                    for _, _, _, d in self._graph.sorted_in_edges(self.name, keys=True, data=True)]
        except Exception as e:
            WARN('[Parser]: An exception occurred with get_input_tensors. Node(%s) %s' % (
                self.name, str(e)))

    def get_output_tensors(self):
        '''Get output tensor of the node.'''
        try:
            return [d['tensor'].value if d['tensor'] is not None else None
                    for _, _, _, d in self._graph.sorted_out_edges(self.name, keys=True, data=True)]
        except Exception as e:
            WARN('[Parser]: An exception occurred with get_input_tensors. Node(%s) %s' % (
                self.name, str(e)))

    def is_all_inputs_const(self):
        '''Determine whether all inputs are constant nodes.'''
        if isinstance(self, ConstLikeOp):
            return True
        else:
            is_const_list = [d['tensor'].is_const for _, _, _, d in self._graph.sorted_in_edges(
                self.name, keys=True, data=True)]
            return True if (is_const_list and all(is_const_list)) else False

    def is_all_outputs_const(self):
        '''Determine whether all outputs are constant nodes.'''
        if isinstance(self, ConstLikeOp):
            return True
        else:
            is_const_list = [d['tensor'].is_const for _, _, _, d in self._graph.sorted_out_edges(
                self.name, keys=True, data=True)]
            return True if (is_const_list and all(is_const_list)) else False

    def get_input_shapes(self):
        '''Returns the shape of all inputs to this op.'''
        try:
            return [list(d['tensor'].value.shape) if d['tensor'].value is not None else d['tensor'].shape
                    for _, _, _, d in self._graph.sorted_in_edges(self.name, keys=True, data=True)]
        except Exception as e:
            WARN('[Parser]: Node(%s) get_input_shapes meets error: %s' %
                 (self.name, str(e)))
            return []

    def get_output_shapes(self):
        '''Returns the shape of all outputs to this op.'''
        try:
            return [list(d['tensor'].value.shape) if d['tensor'].value is not None else None
                    for _, _, _, d in self._graph.sorted_out_edges(self.name, keys=True, data=True)]
        except Exception as e:
            WARN('[Parser]: Node(%s) get_output_shapes meets error:%s' %
                 (self.name, str(e)))
            return []

    def sorted_in_consts(self):
        '''Sort the contents of the constant node in the order of name, attr, and value.'''
        ret = []
        for u, _, in_attr in self._graph.sorted_in_edges(self.name, data=True):
            obj = self._graph.nodes[u]._attr.get('object', None)
            if obj is not None and obj.type in ('Constant', 'TfConst'):
                ret.append((u, in_attr['dst_in_port'], obj.value))
        return ret

    def get_in_ports(self):
        '''Get dst_in_port after sorting.'''
        ports = [d['dst_in_port'] for _, _, _, d in self._graph.sorted_in_edges(
            self.name, keys=True, data=True)]
        return sorted(list(set(ports)))

    def get_out_ports(self):
        '''Get src_out_port after sorting.'''
        ports = [d['src_out_port'] for _, _, _, d in self._graph.sorted_out_edges(
            self.name, keys=True, data=True)]
        return sorted(list(set(ports)))

    def get_inputs_info(self):
        '''Get inputs info about name,value shape,value dtype.'''
        ret = []
        for u, v, k, d in self._graph.sorted_in_edges(self.name, keys=True, data=True):
            pred_node_obj = self._graph.nodes[u]._attr.get('object', None)
            if pred_node_obj is not None:
                if u in self._graph._attr['duplicate_name']:
                    u = self._graph._attr['duplicate_name'][u]
                pre_name_suffix = '' if isinstance(
                    pred_node_obj, OpHasOneOutPort) else '_' + str(d['src_out_port'])
                if d['tensor'].value is not None:
                    ret.append((u + pre_name_suffix, re.sub(r' ', '',
                                                            str(list(d['tensor'].value.shape))), str(d['tensor'].value.dtype)))
        if ret:
            ret = list(zip(*ret))
        return ret

    def get_outputs_info(self):
        '''Get outputs info about name,value shape,value dtype.'''
        ret = []
        info = OrderedDict()
        for u, v, k, d in self._graph.sorted_out_edges(self.name, keys=True, data=True):
            name_suffix = '' if isinstance(
                self, OpHasOneOutPort) else '_' + str(d['src_out_port'])
            info_value = []
            if u in self._graph._attr['duplicate_name']:
                u = self._graph._attr['duplicate_name'][u]
            if (d['tensor'].value is not None and d['tensor'].value.size > 0) \
                    or (d['tensor'].shape is not None and len(d['tensor'].shape) > 0):
                tensor_shape = list(d['tensor'].value.shape)
                info_value = [re.sub(r' ', '', str(tensor_shape)), str(
                    d['tensor'].value.dtype)]
            else:
                tensor_shape = []
                WARN('[Parser]: An exception occurred with get_outputs_info. Got invalid output shape for Node(%s)!' %
                     self.name)
                info_value = [re.sub(r' ', '', str(tensor_shape)), '']
            info.update({u + name_suffix: info_value})
            if len(d['tensor'].min_max) == 2:
                info_value.append('[%f,%f]' % (
                    float(d['tensor'].min_max[0]), float(d['tensor'].min_max[1])))
        if len(info) > 0:
            ret = [(k, *v) for k, v in info.items()]
            ret = list(zip(*ret))
        return ret


class OpHasOneOutPort(Op):
    '''
    Class OpHasOneOutPort inherited from OP.
    All OPs with only one out port must inherit from this class.
    '''

    def __init__(self, graph, attr_dict=None):
        super(OpHasOneOutPort, self).__init__(graph, attr_dict)

    @abc.abstractmethod
    def infer_shape(self):
        '''An abstract method for shape inference.'''
        super(OpHasOneOutPort, self).infer_shape()

    def set_out_tensor(self, tensor_data):
        '''set the out tensor of this op.'''
        try:
            is_const = self.is_all_inputs_const()
            for _, _, d in self._graph.sorted_out_edges(self.name, data=True):
                if d.get('tensor', None) is not None:
                    d['tensor'].value = tensor_data
                    if tensor_data is not None:
                        d['tensor'].shape = d['tensor'].value.shape
                        d['tensor'].is_const = is_const
                else:
                    d['tensor'] = Tensor(value=tensor_data)
        except KeyError as e:
            WARN('[Parser]: Node(%s) meets key error in set_out_tensor (%s)!' %
                 (self.name, str(e)))
        except Exception as e:
            WARN('[Parser]: Node(%s) meets exception in set_out_tensor (%s)!' %
                 (self.name, str(e)))


class OpHasMultipleOutPorts(Op):
    '''
    Class OpHasMultipleOutPorts inherited from OP.
    All OPs with multiple out ports must inherit from this class.
    '''
    @abc.abstractmethod
    def infer_shape(self):
        '''An abstract method for shape inference.'''
        super(OpHasMultipleOutPorts, self).infer_shape()

    def set_out_tensor(self, tensor_data_list):
        '''set the out tensor of this op.'''
        try:
            from ..graph.node_wrap import NodeWrap
            from ..graph.graph_algo import get_valid_node_name
            is_const = self.is_all_inputs_const()
            out_ports = self.get_out_ports()
            if len(tensor_data_list) > len(out_ports):
                for i in range(len(tensor_data_list)):
                    if i not in out_ports:
                        out = get_valid_node_name(
                            self._graph, self.name + '_out_port_' + str(i))
                        self._graph.add_edge(
                            self.name, out, **{'src_out_port': i, 'dst_in_port': 0})
                        NodeWrap(self._graph, out).replace_obj(
                            'Out', {'name': out})
                out_ports = self.get_out_ports()

            out_edges = self._graph.sorted_out_edges(self.name, data=True)
            for i, t in enumerate(tensor_data_list):
                if i in out_ports:
                    cur_port_edges = [
                        e for e in out_edges if e[2]['src_out_port'] == i]
                    for _, _, d in cur_port_edges:
                        if d.get('tensor', None) is not None:
                            d['tensor'].value = t
                            if t is not None:
                                d['tensor'].shape = d['tensor'].value.shape
                                d['tensor'].is_const = is_const
                        else:
                            d['tensor'] = Tensor(value=t, is_const=is_const)
        except KeyError as e:
            WARN('[Parser]: Node(%s) meets key error in set_out_tensor (%s)!' %
                 (self.name, str(e)))
        except Exception as e:
            WARN('[Parser]: Node(%s) meets exception in set_out_tensor (%s)!' %
                 (self.name, str(e)))


class OpHasVariableOutPorts(Op):
    '''
    Class OpHasVariableOutPorts inherited from OP.
    All OPs with variable out ports must inherit from this class.
    '''

    def __init__(self, graph, attr_dict=None):
        super(OpHasVariableOutPorts, self).__init__(graph, attr_dict)

    @abc.abstractmethod
    def infer_shape(self):
        '''An abstract method for shape inference.'''
        super(OpHasVariableOutPorts, self).infer_shape()

    def set_out_tensor(self, tensor_data_list):
        '''set the out tensor of this op.'''
        try:
            from ..graph.node_wrap import NodeWrap
            from ..graph.graph_algo import get_valid_node_name
            is_const = self.is_all_inputs_const()
            if len(tensor_data_list) > 1:
                out_ports = self.get_out_ports()
                if len(tensor_data_list) > len(out_ports):
                    for i in range(len(tensor_data_list)):
                        if i not in out_ports:
                            out = get_valid_node_name(
                                self._graph, self.name + '_out_port_' + str(i))
                            self._graph.add_edge(
                                self.name, out, **{'src_out_port': i, 'dst_in_port': 0})
                            NodeWrap(self._graph, out).replace_obj(
                                'Out', {'name': out})
                    out_ports = self.get_out_ports()
                for _, _, d in self._graph.sorted_out_edges(self.name, data=True):
                    if d.get('tensor', None) is not None:
                        d['tensor'].value = tensor_data_list[out_ports.index(
                            d['src_out_port'])]
                        d['tensor'].shape = d['tensor'].value.shape
                        d['tensor'].is_const = is_const
                    else:
                        d['tensor'] = Tensor(
                            value=tensor_data_list[out_ports.index(d['src_out_port'])])
            else:
                for _, _, d in self._graph.sorted_out_edges(self.name, data=True):
                    if d.get('tensor', None) is not None:
                        d['tensor'].value = tensor_data_list[0]
                        d['tensor'].shape = d['tensor'].value.shape
                        d['tensor'].is_const = is_const
                    else:
                        d['tensor'] = Tensor(value=tensor_data_list[0])
        except KeyError as e:
            WARN('[Parser]: Node(%s) meets key error in set_out_tensor (%s)! ' %
                 (self.name, str(e)))
        except Exception as e:
            WARN('[Parser]: Node(%s) meets exception in set_out_tensor (%s)!' %
                 (self.name, str(e)))


class OpHasMethod(Op):
    '''
    Class OpHasMethod inherited from OP.
    All OPs with methods must inherit this class. Such Ops may have different methods. 
    In the infer_shape method, different functions are called for infer shape according to different methods.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of OpHasMethod class.'''
        return {'method': {'type': AttrType.STRING, 'required': True}}

    def __init__(self, graph, attr_dict=None):
        super(OpHasMethod, self).__init__(graph, attr_dict)
        self.update_attributes(OpHasMethod, attr_dict)

    @property
    def method(self):
        '''Get method for op.'''
        attr = self._attr.get('method', None)
        ret = attr.value.upper()
        if self._attr['method'].options:
            assert ret in self._attr['method'].options, 'method is missing from parameter in OpHasMethod method.'
        return ret

    @method.setter
    def method(self, value):
        '''Set method for op.'''
        assert value in self._attr['method'].options, 'method is missing from parameter in OpHasMethod method.'
        self._attr['method'].value = value

    def write_attrs(self, txt_file):
        '''Write the required attr in IR.'''
        ret = super(OpHasMethod, self).write_attrs(txt_file)
        if ret:
            if self.method and self.type not in ('ArmBasicLSTM', 'ArmGRUv1', 'ArmGRUv3'):
                txt_file.write('method=%s\n' % self.method)
        return ret


class OpHasAxis(Op):
    '''
    Class OpHasAxis inherited from OP.
    All OPs with Axis must inherit from this class.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of OpHasAxis class.'''
        return {'axis': {'type': AttrType.INT, 'default': 0},
                'axes': {'type': AttrType.INTS, 'default': None},
                'keepdims': {'type': AttrType.INT, 'default': 1},
                'new_axis': {'type': AttrType.INT, 'default': 0}
                }

    @staticmethod
    def align_axes(tensor, axes, exp_shape):
        '''Check whether the shape of tensor in axes is 1 or can be broadcast to exp_shape.
        Return None if cannot get tensor with shape exp_shape.'''
        # Make axes a sorted list and make sure it has the same length as exp_shape
        axes = axes if isinstance(axes, (list, np.ndarray)) else [axes]
        axes = sorted(axes)
        assert len(axes) == len(
            exp_shape), 'Invalid exp_shape %s in align_axes' % str(exp_shape)
        # For scalar and valid 1d tensor, broadcast to exp_shape
        in_shape = tensor.shape
        if len(in_shape) in (0, 1) and (tensor.size == 1 or in_shape[0] in (1, exp_shape[-1])):
            return np.broadcast_to(tensor, exp_shape)
        # Reset axes if tensor has same length as axes and axes are contiguous
        if len(in_shape) == len(axes) \
                and not any(diff != 1 for diff in np.diff(axes)):
            axes = list(range(len(in_shape)))
        elif len(in_shape) <= axes[-1]:
            return None
        # Get actual output shape and axes at which tensor needs to be sliced
        sliced_axes = []
        out_shape = []
        exp_shape_idx = 0
        for idx, shape in enumerate(in_shape):
            if (idx not in axes and shape != 1) \
                    or (idx in axes and shape not in (1, exp_shape[exp_shape_idx])):
                sliced_tensor = tensor.take(0, axis=idx)
                is_repeated = all([FLOAT_EQUAL(sliced_tensor, tensor.take(
                    num, axis=idx)) for num in range(1, shape)])
                if not is_repeated:
                    return None
                sliced_axes.append(idx)
            if idx in axes:
                out_shape.append(1 if idx in sliced_axes else shape)
                exp_shape_idx = exp_shape_idx + 1
        # Get returned tensor with shape exp_shape
        ret_tensor = tensor
        for axis in reversed(sliced_axes):
            ret_tensor = ret_tensor.take(0, axis=axis)
        if len(ret_tensor.shape) != len(axes):
            ret_tensor = np.reshape(ret_tensor, out_shape)
        if any([ret_tensor.shape[idx] not in (1, exp_shape[idx]) for idx in range(len(axes))]):
            return None
        ret_tensor = np.broadcast_to(ret_tensor, exp_shape)
        return ret_tensor

    @staticmethod
    def broadcast_to(tensor, ref_shape, axis):
        '''Return the tensor after broadcast.'''
        if len(tensor.shape) < len(ref_shape):
            ndims_need_expand = len(ref_shape) - int(axis) - len(tensor.shape)
            if ndims_need_expand > 0:
                new_shape = tensor.shape + [1] * ndims_need_expand
                ret = np.reshape(tensor, newshape=new_shape)
            else:
                ret = tensor
        else:
            ret = tensor
        return ret

    @staticmethod
    def expand_to(tensor, axes, shape_len):
        '''Return the tensor after expanding to its shape with length shape_len.
        Except the dimension in axes, the other dimensions will be with shape 1.'''
        assert len(tensor.shape) == len(
            axes), 'Tensor shape length is not equal to the length of axes in expand_to'
        t_idx = 0
        new_shape = []
        for idx in range(shape_len):
            if idx in axes:
                new_shape.append(tensor.shape[t_idx])
                t_idx = t_idx + 1
            else:
                new_shape.append(1)
        ret_tensor = np.reshape(tensor, new_shape)
        return ret_tensor

    @staticmethod
    def make_axes_non_negative(axes, ref_shape_length, need_extend=False):
        '''If attr axes is negative, return non-negative axes.'''
        np_axes = np.array(axes, np.int64)
        negative_axes = np_axes < 0
        if np.any(negative_axes):
            len_shape = ref_shape_length + 1 if need_extend else ref_shape_length
            np_axes[negative_axes] += len_shape
        return np_axes.tolist()

    def __init__(self, graph, attr_dict=None):
        super(OpHasAxis, self).__init__(graph, attr_dict)
        self.update_attributes(OpHasAxis, attr_dict)

    def __getattr__(self, item):
        '''Returns the OpHasAxis object property value.'''
        ret = None
        try:
            if item == 'axis':
                ret = int(self.__dict__['_attr'][item].value)
            elif item == 'axes':
                ret = list(self.__dict__['_attr'][item].value)
            elif item in ('keepdims', 'new_axis'):
                ret = bool(self.__dict__['_attr'][item].value)
        except:
            ret = None
        if ret is None:
            ret = super(OpHasAxis, self).__getattr__(item)
        return ret

    def __setattr__(self, item, value):
        '''Used to set the attribute value of the OpHasAxis object, the attribute does not necessarily exist.'''
        try:
            if item in ('keepdims', 'new_axis'):
                self.__dict__['_attr'][item].value = int(value)
            else:
                super(OpHasAxis, self).__setattr__(item, value)
        except:
            super(OpHasAxis, self).__setattr__(item, value)

    @abc.abstractmethod
    def infer_shape(self):
        '''An abstract method for shape inference.'''
        super(OpHasAxis, self).infer_shape()
        if len(self.get_input_shapes()) >= 1 and self.get_input_shapes()[0]:
            need_extend = False
            if self.type in ('OneHot', 'TfOneHot', 'ConcatFromSequence', 'TfPack'):
                if self.type == 'ConcatFromSequence' and not self.new_axis:
                    need_extend = False
                else:
                    need_extend = True
            if self.axis is not None and self.axis < 0:
                self.axis = OpHasAxis.make_axes_non_negative(
                    self.axis, len(self.get_input_shapes()[0]), need_extend)
            elif self.axes is not None and any([int(x) < 0 for x in self.axes]):
                if self.type == 'Unsqueeze':
                    self.axes = OpHasAxis.make_axes_non_negative(
                        self.axes, len(self.get_input_shapes()[0])+len(self.axes))
                else:
                    self.axes = OpHasAxis.make_axes_non_negative(
                        self.axes, len(self.get_input_shapes()[0]), need_extend)

    def write_attrs(self, txt_file):
        '''Write the required attr in IR.'''
        ret = super(OpHasAxis, self).write_attrs(txt_file)
        if ret:
            if 'axes' in self._attr and self.axes:
                self.axes = sorted(self.axes.tolist() if isinstance(
                    self.axes, np.ndarray) else list(self.axes))
                txt_file.write('axis=[%s]\n' % num_list_to_string(self.axes))
            elif 'axis' in self._attr:
                txt_file.write('axis=%s\n' % num_list_to_string(self.axis.tolist()
                                                                if isinstance(self.axis, np.ndarray)
                                                                else self.axis))
        return ret


class LayoutConcernedOp(Op):
    '''
    Class LayoutConcernedOp inherited from OP.
    Some ops involved in tweaking the layout must inherit from this class.
    '''
    pass


class LayoutUnawareOp(Op):
    '''
    Class LayoutUnawareOp inherited from OP.
    Some layout-insensitive OPs, such as math ops, must inherit from this class.
    '''
    pass


class OpHasPaddingStrides(LayoutConcernedOp):
    """
    Class OpHasPaddingStrides inherited from LayoutConcernedOp class.
    OPs that contain pad and stride parameters need to inherit from this class, 
    usually these ops are ops related to convolutional layers.
    Attributes:
        kernel_shape: The shape of the convolution kernel in the conv type op.

    The following gives the meaning of each input and output of the conv type op under different frameworks.

        TF: conv:           [filter_height, filter_width, in_channels, out_channels]
            conv1d:         [filter_width, out_channels, in_channels]
            depthwise conv: [filter_height, filter_width, in_channels(out), channel_multiplier]
            deconv:         [filter_height, filter_width, output_channels, in_channels]
            fc:             [input_channel, output_channel]

        TL: conv:           [out_channels, filter_height, filter_width, in_channels]
            depthwise conv: [channel_multiplier, filter_height, filter_width, in_channels] or [1, filter_height, filter_width, in_channels *channel_multiplier]
            deconv:         [output_channels, filter_height, filter_width, in_channels]
            fc:             [output_channel, input_channel]

        ONNX:  (M x C/group x kH x kW)
            conv:           [out_channels, in_channels, filter_height, filter_width]
            conv1d:         [out_channels, in_channels, filter_width]
            depthwise conv: [in/out_channels, channel_multiplier, filter_height, filter_width]
            deconv:         [in_channels, output_channels, filter_height, filter_width]
            fc:             [output_channel, input_channel]

        torch: conv         [out_channels, in_channels, filter_height, filter_width]
            depthwise conv: [in_channels(out), channel_multiplier, filter_height, filter_width]

        caffe: conv         [out_channels, in_channels, filter_height, filter_width]
            deconv:      [in_channels, out_channels, filter_height, filter_width]

        IR: conv:           [out_channels, filter_height, filter_width, in_channels]
            depthwise conv: [in_channels(out), filter_height, filter_width, channel_multiplier]
            deconv:         [output_channels/group, filter_height, filter_width, in_channels]
            fc:             [output_channel, input_channel]
    """

    @classmethod
    def attributes(cls):
        '''return attributes of OpHasPaddingStrides class.'''
        return {'auto_pad': {'type': AttrType.STRING, 'default': 'NOTSET', 'options': ['NOTSET', 'SAME_UPPER', 'SAME_LOWER', 'VALID']},
                'dilations': {'type': AttrType.INTS},
                'kernel_shape': {'type': AttrType.INTS},
                'pads': {'type': AttrType.INTS, 'required': False},
                'strides': {'type': AttrType.INTS}
                }

    @staticmethod
    def cal_pads(in_shape, out_shape, strides, kernel_shape, auto_pad, dilations=None, is_transpose=False, zero_minimum=False, out_padding=None):
        '''Calculate the pad parameters of the OP according to parameters such as in_shape, out_shape, strides, kernel_shape, etc.'''
        if not dilations:
            dilations = [1] * len(in_shape)
        if not out_padding:
            out_padding = [0] * len(in_shape)
        if is_transpose:
            in_shape, out_shape = copy.deepcopy(
                out_shape), copy.deepcopy(in_shape)
        params = [in_shape, out_shape, strides,
                  kernel_shape, dilations, out_padding]
        in_shape, out_shape, strides, kernel_shape, dilations, out_padding = [
            np.array(p, np.int64) for p in params]

        pads = (out_shape - 1) * strides + out_padding + \
            (kernel_shape - 1) * dilations + 1 - in_shape

        if zero_minimum:
            pads = np.maximum(pads, 0)

        if auto_pad == 'SAME_UPPER':
            pad_head = (np.abs(pads) // 2) * np.sign(pads)
            pad_tail = pads - pad_head
        else:
            pad_tail = (np.abs(pads) // 2) * np.sign(pads)
            pad_head = pads - pad_tail
        return [*pad_head.tolist(), *pad_tail.tolist()]

    @staticmethod
    def onnx_to_torch(pads):
        '''Convert the pad parameter under the onnx framework to the pad under the torch framework.'''
        paddings = np.array(pads, np.int64)
        dims = paddings.size // 2
        paddings = np.reshape(paddings, newshape=(2, dims))
        ret = [paddings[:, d].tolist()
               for d in sorted(range(dims), reverse=True)]
        return extend_lists(ret)

    @staticmethod
    def onnx_to_tf(pads):
        '''Convert the pad parameter under the onnx framework to the pad under the tensorflow framework.'''
        assert len(
            pads) % 2 == 0, 'The length of pads is invalid in OpHasPaddingStrides onnx_to_tf.'
        np_pads = np.transpose(np.reshape(np.array(pads, np.int64), (2, -1)))
        space_dims = len(pads) // 2
        ret = np.zeros((space_dims+2, 2), np.int64)
        ret[1:1+space_dims, :] = np_pads
        return ret

    @staticmethod
    def tf_to_onnx(paddings, as_full=False):
        '''Convert the pad parameter under the tensorflow framework to the pad under the onnx framework.'''
        assert paddings.shape[-1] == 2, 'The shape of paddings is invalid in OpHasPaddingStrides tf_to_onnx.'
        paddings = np.transpose(paddings)
        if not as_full:
            paddings = paddings[:, 1:-1]
        return paddings.flatten().tolist()

    def __init__(self, graph, attr_dict=None):
        super(OpHasPaddingStrides, self).__init__(graph, attr_dict)
        self.update_attributes(OpHasPaddingStrides, attr_dict)
        if not self.kernel_shape \
                and isinstance(self, OnnxOp) \
                and isinstance(self, OpHasPaddingStrides) \
                and isinstance(self, OpHasWeights):
            if self.weights is not None and len(self.weights.shape) == 4:
                self.kernel_shape = list(self.weights.shape[2:])

    def __getattr__(self, item):
        '''Returns the OP object property value.'''
        ret = None
        try:
            if item in ('dilations', 'strides'):
                ret = self.__dict__['_attr'][item].value
                if not ret:
                    ret = [1] * len(self.__dict__['_attr']
                                    ['kernel_shape'].value)
                    self.__dict__['_attr'][item].value = ret
            elif item == 'pads':
                ret = self.__dict__['_attr'][item].value
                if not ret:
                    ret = [0, 0] * len(self.__dict__['_attr']
                                       ['kernel_shape'].value)
                    self.__dict__['_attr'][item].value = ret
        except:
            ret = None
        if ret is None:
            ret = super(OpHasPaddingStrides, self).__getattr__(item)
        return ret

    @abc.abstractmethod
    def infer_shape(self):
        '''An abstract method for shape inference.'''
        super(OpHasPaddingStrides, self).infer_shape()

    def write_attrs(self, txt_file):
        '''Write the required attr in IR.'''
        ret = super(OpHasPaddingStrides, self).write_attrs(txt_file)
        if ret:
            if self.kernel_shape is not None:
                txt_file.write('kernel_x=%d\n' % self.kernel_shape[-1])
                txt_file.write('kernel_y=%d\n' % self.kernel_shape[-2])
                if len(self.kernel_shape) == 3:
                    txt_file.write('kernel_z=%d\n' % self.kernel_shape[-3])
            else:
                WARN('[Parser]: Invalid kernel_shape for %s Op (%s) in write_attrs!' %
                     (self.type, self.name))

            if self.strides is not None:
                txt_file.write('stride_x=%d\n' % self.strides[-1])
                txt_file.write('stride_y=%d\n' % self.strides[-2])
                if len(self.strides) == 3:
                    txt_file.write('stride_z=%d\n' % self.strides[-3])
            else:
                WARN('[Parser]: Invalid strides for %s Op (%s) in write_attrs!' %
                     (self.type, self.name))

            if self.tf_pads is not None:
                if self.tf_pads.shape[0] == 4:
                    txt_file.write('pad_left=%d\n' % self.tf_pads[2, 0])
                    txt_file.write('pad_right=%d\n' % self.tf_pads[2, 1])
                    txt_file.write('pad_top=%d\n' % self.tf_pads[1, 0])
                    txt_file.write('pad_bottom=%d\n' % self.tf_pads[1, 1])
                else:
                    txt_file.write('pad_x_begin=%d\n' % self.tf_pads[3, 0])
                    txt_file.write('pad_x_end=%d\n' % self.tf_pads[3, 1])
                    txt_file.write('pad_y_begin=%d\n' % self.tf_pads[2, 0])
                    txt_file.write('pad_y_end=%d\n' % self.tf_pads[2, 1])
                    txt_file.write('pad_z_begin=%d\n' % self.tf_pads[1, 0])
                    txt_file.write('pad_z_end=%d\n' % self.tf_pads[1, 1])
            else:
                WARN('[Parser]: Invalid pads for %s Op (%s) in write_attrs!' %
                     (self.type, self.name))

            if self.dilations is not None:
                txt_file.write('dilation_x=%d\n' % self.dilations[-1])
                txt_file.write('dilation_y=%d\n' % self.dilations[-2])
                if len(self.dilations) == 3:
                    txt_file.write('dilation_z=%d\n' % self.dilations[-3])
            else:
                WARN('[Parser]: Invalid dilations for %s Op (%s) in write_attrs!' %
                     (self.type, self.name))
        return ret

    @property
    def torch_pads(self):
        '''Returns the pad parameter of the object under the pytorch framework.'''
        return OpHasPaddingStrides.onnx_to_torch(self.pads)

    @property
    def tf_pads(self):
        '''Returns the pad parameter of the object under the tensorflow framework.'''
        return OpHasPaddingStrides.onnx_to_tf(self.pads)


class OpHasWeights(Op):
    '''
    Class OpHasWeights inherited from OP.
    Some OPs with weight parameter must inherit this class, such as BN, LSTM, etc.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of OpHasWeights class.'''
        return {'weights': {'type': AttrType.TENSOR, 'default': None},
                'weights_offset': {'type': AttrType.INT, 'default': -1},
                'weights_min_max': {'type': AttrType.FLOATS, 'default': []}
                }

    @classmethod
    def perm_ir_to_onnx(cls):
        '''Convert the perm under the IR framework to the perm under the onnx framework.'''
        return None

    @classmethod
    def perm_onnx_to_ir(cls):
        '''Convert the perm under the Onnx framework to the perm under the IR framework.'''
        return Op.cal_inverse_perm(cls.perm_ir_to_onnx())

    @classmethod
    def perm_ir_to_tf(cls):
        '''Convert the perm under the IR framework to the perm under the TF framework.'''
        return [1, 0]

    @classmethod
    def perm_lite_to_onnx(cls):
        '''Convert the perm under the TF lite framework to the perm under the onnx framework.'''
        return None

    @classmethod
    def perm_caffe_to_onnx(cls, dim=4):
        '''Convert the perm under the caffe framework to the perm under the onnx framework.'''
        return None

    @classmethod
    def perm_tf_to_onnx(cls):
        '''Convert the perm under the TF framework to the perm under the onnx framework.'''
        return None

    @classmethod
    def perm_onnx_to_lite(cls):
        '''Convert the perm under the Onnx framework to the perm under the TF lite framework.'''
        return Op.cal_inverse_perm(cls.perm_lite_to_onnx())

    @classmethod
    def perm_lite_to_tf(cls):
        '''Convert the perm under the TF lite framework to the perm under the TF framework.'''
        return None

    @classmethod
    def perm_tf_to_lite(cls):
        '''Convert the perm under the TF framework to the perm under the TF lite framework.'''
        return Op.cal_inverse_perm(cls.perm_tf_to_lite())

    def __init__(self, graph, attr_dict=None):
        super(OpHasWeights, self).__init__(graph, attr_dict)
        self.update_attributes(OpHasWeights, attr_dict)

    def write_attrs(self, txt_file):
        '''Write the required attr in IR.'''
        ret = super(OpHasWeights, self).write_attrs(txt_file)
        if ret and self.weights is not None:
            txt_file.write('weights_type=%s\n' % str(self.weights.dtype))
            txt_file.write('weights_offset=%d\n' % self.weights_offset)
            txt_file.write('weights_size=%d\n' %
                           (self.weights.size * self.weights.dtype.itemsize))
            txt_file.write('weights_shape=[%s]\n' % num_list_to_string(
                list(self.weights.shape)))
            if self.weights_min_max:
                txt_file.write('weights_range=[%s]\n' % num_list_to_string(
                    [float(np.min(m) if i == 0 else np.max(m)) for i, m in enumerate(self.weights_min_max)]))
        return ret

    def write_weights(self, bin_file):
        '''Write the weight attr in IR bin file.'''
        ret = True
        if not bin_file.closed and bin_file.mode == 'wb':
            if self.weights is not None and self.weights_offset >= 0:
                start = bin_file.tell()
                assert start == self.weights_offset, 'weights offset not match! layer name: %s, %d' % (
                    self.name, self.weights_offset)
                self.weights.tofile(bin_file)
                end = bin_file.tell()
                if not (self.weights.dtype.itemsize * int(np.prod(self.weights.shape)) == end - start):
                    ERROR(
                        '[Parser]: Node(%s) write weights to bin error in write_weights!' % self.name)
            else:
                WARN(
                    '[Parser]: Invalid weights for Node %s in write_weights!' % self.name)
        else:
            FATAL('[Parser]: Invalid file to write weights for Node(%s) in write_weights!' %
                  (self.name))
        return ret


class OpHasBiases(Op):
    '''
    Class OpHasBiases inherited from OP.
    Some OPs with weight parameter must inherit this class, such as GRU, LSTM, etc.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of OpHasBiases class.'''
        return {'biases': {'type': AttrType.TENSOR, 'default': None},
                'biases_offset': {'type': AttrType.INT, 'default': -1}
                }

    def __init__(self, graph, attr_dict=None):
        super(OpHasBiases, self).__init__(graph, attr_dict)
        self.update_attributes(OpHasBiases, attr_dict)

    def write_attrs(self, txt_file):
        '''Write the required attr in IR.'''
        ret = super(OpHasBiases, self).write_attrs(txt_file)
        if ret and self.biases is not None:
            txt_file.write('biases_type=%s\n' % str(self.biases.dtype))
            txt_file.write('biases_offset=%d\n' % self.biases_offset)
            txt_file.write('biases_size=%d\n' %
                           (self.biases.size * self.biases.dtype.itemsize))
            txt_file.write('biases_shape=[%s]\n' %
                           num_list_to_string(list(self.biases.shape)))
        return ret

    def write_biases(self, bin_file):
        '''Write the biases attr in IR bin file.'''
        ret = True
        if not bin_file.closed and bin_file.mode == 'wb':
            if self.biases is not None and self.biases_offset >= 0:
                start = bin_file.tell()
                assert start == self.biases_offset, 'biases offset not match! layer name: %s, %d' % (
                    self.name, self.biases_offset)
                self.biases.tofile(bin_file)
                end = bin_file.tell()
                if not (self.biases.dtype.itemsize * int(np.prod(self.biases.shape)) == end - start):
                    ERROR('[Parser]: Node(%s) write biases to bin error in write_biases!' %
                          self.name)
        else:
            FATAL('[Parser]: Invalid file to write biases for Node(%s) in write_biases!' %
                  (self.name))
        return ret


class BaseLinearOp(OpHasBiases, OpHasWeights, OpHasOneOutPort):
    '''
    Class BaseLinearOp inherited from OpHasBiases, OpHasWeights, OpHasOneOutPort class.
    All OPs with linear structure must inherit BaseLinearOp. For example, BN layer, fully connected layer.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of BaseLinearOp class.'''
        return {'num_output': {'type': AttrType.INT, 'default': None}}

    @classmethod
    def perm_lite_to_tf(cls):
        '''Convert the perm under the TF lite framework to the perm under the TF framework.'''
        return [1, 0]

    @classmethod
    def cast_in_ports(cls):
        '''Returns the port index to which the cast needs to be added and the cast dtype to be converted.'''
        return {0: 'float32'}

    def __init__(self, graph, attr_dict=None):
        super(BaseLinearOp, self).__init__(graph, attr_dict)
        self.update_attributes(BaseLinearOp, attr_dict)

    def write_attrs(self, txt_file):
        '''Write the required attr in IR.'''
        ret = super(BaseLinearOp, self).write_attrs(txt_file)
        if ret:
            if self.type != 'ArmBatchNorm':
                txt_file.write('num_output=%d\n' % self.num_output)
        return ret

    def __getattr__(self, item):
        ret = None
        try:
            if item == 'num_output':
                ret = self.__dict__['_attr'][item].value
                if ret is None:
                    if self.weights is not None:
                        if self.type == 'ConvTranspose':
                            ret = self.weights.shape[1] * self.group
                        else:
                            ret = self.weights.shape[0]
                    elif isinstance(self, OpHasBiases) and self.biases is not None:
                        ret = self.biases.shape[0]
                    if ret is not None:
                        self.__dict__['_attr'][item].value = ret
        except:
            ret = None
        if ret is None:
            ret = super(BaseLinearOp, self).__getattr__(item)
        return ret


class BaseConvOp(OpHasPaddingStrides, BaseLinearOp, LayoutConcernedOp):
    '''
    Class BaseConvOp inherited from OpHasPaddingStrides, BaseLinearOp, LayoutConcernedOp class.
    All OPs of type conv must inherit BaseConvOp..
    '''
    @staticmethod
    def cal_out_shape(in_shape, pads, strides, kernel_shape, auto_pad, dilations=None, data_format='NCHW'):
        '''Calculate the output shape of the OP according to in_shape, pads, strides, kernel_shape, auto_pad and other parameters.'''
        if dilations is None:
            dilations = [1] * len(kernel_shape)
        params = [in_shape, strides, kernel_shape, dilations]
        in_shape, strides, kernel_shape, dilations = [
            np.array(p, np.int64) for p in params]
        if len(pads) in (2, 4, 6):
            padding = np.reshape(np.array(pads, np.int64), (2, -1))
            padding = np.sum(padding, axis=0, keepdims=False)
            if data_format == 'NHWC':
                if auto_pad == 'NOTSET':
                    in_shape += padding
                if auto_pad in ('SAME_UPPER', 'SAME_LOWER'):
                    out_shape = np.ceil(in_shape / strides).astype(np.int64)
                else:
                    out_shape = np.ceil(
                        (in_shape - (kernel_shape - 1) * dilations) / strides).astype(np.int64)
            else:
                if auto_pad == 'NOTSET':
                    in_shape += padding
                    out_shape = np.floor(
                        (in_shape - dilations * (kernel_shape - 1) - 1) / strides + 1).astype(np.int64)
                elif auto_pad in ('SAME_UPPER', 'SAME_LOWER'):
                    out_shape = np.ceil(in_shape / strides).astype(np.int64)
                else:
                    out_shape = np.floor(
                        (in_shape - dilations * (kernel_shape - 1) - 1) / strides + 1).astype(np.int64)
            ret = out_shape.tolist()
        else:
            WARN('[Parser]: Invalid pads len %s in cal_out_shape!' % (str(pads)))
            ret = []
        return ret

    @classmethod
    def attributes(cls):
        '''return attributes of BaseConvOp class.'''
        return {'group': {'type': AttrType.INT, 'default': 1}}

    def __init__(self, graph, attr_dict=None):
        super(BaseConvOp, self).__init__(graph, attr_dict)
        self.update_attributes(BaseConvOp, attr_dict)
        if self.biases is None \
                and getattr(self, 'num_output', None) is not None:
            if 'ConvInteger' in self.type:
                self.biases = np.zeros((self.num_output,), np.int32)
            else:
                self.biases = np.zeros((self.num_output,), np.float32)

    def write_attrs(self, txt_file):
        '''Write the required attr in IR.'''
        ret = super(BaseConvOp, self).write_attrs(txt_file)
        if ret:
            txt_file.write('group=%d\n' % self.group)
        return ret


class BaseActivationOp(OpHasOneOutPort):
    '''
    Class BaseActivationOp inherited from OpHasOneOutPort class.
    All OPs of type activation must inherit BaseConvOp..
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of BaseActivationOp class.'''
        return {'activations': {'type': AttrType.STRING,
                                'default': 'NONE',
                                'options': ['NONE', 'CLIP', 'CELU', 'RELU', 'RELU6', 'LEAKYRELU', 'PRELU', 'RELU_N1_TO_1', 'SIGMOID', 'TANH', 'SOFTSIGN', 'SELU'],
                                'required': True
                                },
                'negative_slope': {'type': AttrType.TENSOR},
                'negative_slope_offset':  {'type': AttrType.INT, 'default': -1},
                'clip_min': {'type': AttrType.FLOAT, 'default': None},
                'clip_max': {'type': AttrType.FLOAT, 'default': None}
                }

    def __init__(self, graph, attr_dict=None):
        super(BaseActivationOp, self).__init__(graph, attr_dict)
        self.update_attributes(BaseActivationOp, attr_dict)
        self.update_activation(attr_dict)

    @abc.abstractmethod
    def infer_shape(self):
        '''An abstract method for shape inference.'''
        super(BaseActivationOp, self).infer_shape()

    def write_attrs(self, txt_file):
        '''Write the required attr in IR.'''
        ret = super(BaseActivationOp, self).write_attrs(txt_file)
        if ret:
            txt_file.write('with_activation=%s\n' % self.activations)
            if self.activations != 'NONE':
                if self.activations in ('CLIP', 'LEAKYRELU', 'PRELU', 'RELU', 'RELU6', 'SELU'):
                    if self.activations == 'CLIP':
                        txt_file.write('clip_min=%s\n' % str(self.clip_min))
                        txt_file.write('clip_max=%s\n' % str(self.clip_max))
                    elif self.activations == 'LEAKYRELU':
                        txt_file.write('negative_slope_type=%s\n' % 'float32')
                        txt_file.write('negative_slope_value=%1.6f\n' %
                                       float(self.negative_slope))
                    elif self.activations == 'PRELU':
                        txt_file.write('negative_slope_type=%s\n' % 'float32')
                        txt_file.write('negative_slope_offset=%d\n' %
                                       self.negative_slope_offset)
                        txt_file.write('negative_slope_size=%d\n' % (
                            self.negative_slope.size * self.negative_slope.dtype.itemsize))
                        txt_file.write('negative_slope_shape=[%s]\n' % num_list_to_string(
                            list(self.negative_slope.shape)))
                    if self.activations == 'SELU':
                        txt_file.write('alpha=%s\n' % str(self.alpha))
                        txt_file.write('gamma=%s\n' % str(self.gamma))
                    elif self.activations == 'RELU6':
                        pass
                else:
                    WARN(
                        '[Parser]: Node(%s) Meets invalid activation type in write_attrs!' % self.name)
        return ret

    def write_negative_slope(self, bin_file):
        '''Write the negative_slope attr in IR binfile.'''
        ret = True
        if not bin_file.closed and bin_file.mode == 'wb':
            if self.negative_slope is not None \
                    and np.ndim(self.negative_slope) > 0 \
                    and self.negative_slope_offset >= 0:
                start = bin_file.tell()
                assert start == self.negative_slope_offset, 'negative_slope offset not match! layer name: %s, %d' % (
                    self.name, self.negative_slope_offset)
                self.negative_slope.tofile(bin_file)
                end = bin_file.tell()
                if not (self.negative_slope.dtype.itemsize * int(np.prod(self.negative_slope.shape)) == end - start):
                    ERROR(
                        '[Parser]: Node(%s) write negative_slope to bin error in write_negative_slope!' % self.name)
            else:
                pass
        else:
            FATAL(
                '[Parser]: Invalid file to write negative_slope for Node(%s) in write_negative_slope!' % self.name)
        return ret

    @property
    def activations(self):
        '''Get the activations attr of the op.'''
        attr = self._attr.get('activations', None)
        return attr.value if attr is not None else 'NONE'

    @activations.setter
    def activations(self, value):
        '''Set the activations attr of the op.'''
        assert value.upper() in BaseActivationOp.attributes()[
            'activations']['options'], 'value is not in the parameters of BaseActivationOp.'
        self._attr['activations'].value = value

    @property
    def negative_slope(self):
        '''Get the negative_slope attr of the op.'''
        if self.activations in ('LEAKYRELU', 'PRELU'):
            attr = self._attr.get('negative_slope', None)
            return attr.value if attr is not None else None
        else:
            return None

    @negative_slope.setter
    def negative_slope(self, value):
        '''Set the negative_slope attr of the op.'''
        self.update_attr_tensor('negative_slope', value)

    @property
    def negative_slope_offset(self):
        '''Get the negative_slope attr of the op.'''
        return self._attr['negative_slope_offset'].value

    @negative_slope_offset.setter
    def negative_slope_offset(self, value):
        '''Set the negative_slope attr of the op.'''
        self._attr['negative_slope_offset'].value = value

    @property
    def clip_min(self):
        '''Get the clip_min attr of the op.'''
        return self.get_attr_by_key('clip_min')

    @clip_min.setter
    def clip_min(self, value):
        '''Set the clip_min attr of the op.'''
        self._attr['clip_min'].value = value

    @property
    def clip_max(self):
        '''Get the clip_max attr of the op.'''
        return self.get_attr_by_key('clip_max')

    @clip_max.setter
    def clip_max(self, value):
        '''Set the clip_max attr of the op.'''
        self._attr['clip_max'].value = value

    def cal_activation(self, tensor, weights=None):
        '''Returns different activation functions based on the parameters of activations.'''
        act = self.activations
        assert act in BaseActivationOp.attributes(
        )['activations']['options'], 'act is not in the parameters of BaseActivationOp.'
        if not isinstance(tensor, np.ndarray):
            tensor = np.asarray(tensor)
        torch_tensor = torch.from_numpy(tensor)
        if act == 'RELU':
            ret = torch.nn.functional.relu(torch_tensor, inplace=False).numpy()
        elif act == 'RELU6':
            ret = torch.nn.functional.relu6(
                torch_tensor, inplace=False).numpy()
        elif act == 'LEAKYRELU':
            ret = torch.nn.functional.leaky_relu(torch_tensor, negative_slope=float(
                self.negative_slope), inplace=False).numpy()
        elif act == 'PRELU':
            if weights is None:
                weights = self.negative_slope
            ret = (torch.clip(torch_tensor, min=0) + torch.clip(torch_tensor,
                                                                max=0) * torch.from_numpy(weights)).numpy()
        elif act == 'CLIP':
            ret = torch.clamp(torch_tensor, min=self.clip_min,
                              max=self.clip_max).numpy()
        elif act == 'CELU':
            a = self.alpha
            x = torch_tensor
            x_0 = torch.tensor(0)
            ret = (torch.maximum(x_0, x) + torch.minimum(x_0,
                                                         a * (torch.exp(x / a) - 1.0))).numpy()
        else:
            ret = tensor
        return ret

    def update_activation(self, attr_dict):
        '''Update parameters based on the type of activations.'''
        if self.activations == 'NONE':
            self.activations = attr_dict.get('activations', 'NONE')
            if self.activations == 'LEAKYRELU' and self.negative_slope is None and 'alpha' in attr_dict:
                self.negative_slope = np.array(attr_dict['alpha'], np.float32)
            elif self.activations == 'PRELU' and attr_dict.get('negative_slope', None) is not None:
                self.negative_slope = attr_dict['negative_slope'].copy()
            elif self.activations == 'CLIP':
                self.clip_min = attr_dict['clip_min']
                self.clip_max = attr_dict['clip_max']


class BaseReluOp(BaseActivationOp):
    '''
    Class BaseReluOp inherited from BaseActivationOp class.
    All OPs of type relu must inherit BaseReluOp.
    '''
    pass


class BaseRnnOp(OpHasMethod, OpHasVariableOutPorts):
    '''
    Class BaseRnnOp inherited from OpHasMethod, OpHasVariableOutPorts class.
    All OPs of type rnn must inherit BaseRnnOp.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of BaseRnnOp class.'''
        return {
            'input_size': {'type': AttrType.INT, 'default': 0},
            'hidden_size': {'type': AttrType.INT, 'default': 0},
            'time_steps': {'type': AttrType.INT, 'default': 0},
            'direction': {'type': AttrType.STRING, 'default': 'forward', 'options': ['forward', 'reverse', 'bidirectional']},
            'activations': {'type': AttrType.STRINGS, 'required': False, 'default': []},
            'method': {'required': False, 'default': 'Y', 'options': ['Y', 'C', 'H', 'YH', 'YC', 'CH', 'YCH']}
        }

    def __init__(self, graph, attr_dict=None):
        super(BaseRnnOp, self).__init__(graph, attr_dict)
        self.update_attributes(BaseRnnOp, attr_dict)

    def write_attrs(self, txt_file):
        '''Write the required attr in IR.'''
        ret = super(BaseRnnOp, self).write_attrs(txt_file)
        if ret:
            if self.time_steps > 0:
                txt_file.write('time_steps=%d\n' % self.time_steps)
            if self.input_size > 0:
                txt_file.write('input_size=%d\n' % self.input_size)
            if self.hidden_size > 0:
                txt_file.write('cell_size=%d\n' % self.hidden_size)
            if len(self.activations) > 0:
                txt_file.write('activations=[%s]\n' %
                               string_list_to_string(self.activations))
            txt_file.write('direction=%s\n' % self.direction)
        return ret


class OpHasSubGraph(OpHasVariableOutPorts):
    '''
    Class OpHasSubGraph inherited from OpHasVariableOutPorts class.
    OP with subgraph must inherit OpHasSubGraph.
    '''
    pass


class ConstLikeOp(Op):
    '''
    Class ConstLikeOp inherited from Op class.
    Ops like const must inherit ConstLikeOp.
    '''
    pass


class OpNeedBroadcast(Op):
    '''
    Class OpNeedBroadcast inherited from Op class.
    Ops need brocast must inherit ConstLikeOp.
    '''
    @staticmethod
    def cal_reshape_and_tile(input_shapes, match_from_left=False):
        '''We implement brocast by adding reshape and tile, and this function calculates the parameters of reshape and tile.'''
        ret = []
        if len(input_shapes) >= 2:
            max_rank = max([len(s) for s in input_shapes])
            max_dims_array = np.array(
                [list(s) for s in input_shapes if len(s) == max_rank])
            if max_dims_array.size > 0:
                max_dims = np.max(max_dims_array, axis=0,
                                  keepdims=False).tolist()
            else:
                max_dims = []
            new_dims_dict = {}
            for r in range(max_rank-1, 0, -1):
                for idx, s in enumerate(input_shapes):
                    if len(s) == r and len(s) < len(max_dims):
                        dim_diff = len(max_dims) - len(s)
                        range_list = list(range(0, dim_diff+1)) if match_from_left \
                            else list(range(dim_diff, -1, -1))
                        offset = -1
                        for o in range_list:
                            if all([sd == max_d for sd, max_d in zip(s, max_dims[o:])]):
                                offset = o
                                break
                            if all([sd == max_d or sd == 1 for sd, max_d in zip(s, max_dims[o:])]):
                                offset = o
                                break
                            if all([sd == 1 or max_d == 1 for sd, max_d in zip(s, max_dims[o:])]):
                                offset = o
                                break
                            if all([sd == max_d or sd == 1 or max_d == 1 for sd, max_d in zip(s, max_dims[o:])]):
                                offset = o
                                break
                        if offset == -1:
                            WARN(
                                '[Parser]: Meets invalid input shape for broadcasting in cal_reshape_and_tile!')
                            break
                        else:
                            cur_full_dim = [1] * offset + \
                                list(s) + [1] * (dim_diff - offset)
                            new_dims_dict.update({idx: cur_full_dim})
                            max_dims = np.max(
                                np.array([cur_full_dim, max_dims]), axis=0, keepdims=False).tolist()

            reshape_dims, reps = [], []
            for i, in_shape in enumerate(input_shapes):
                if len(in_shape) == 0:
                    reshape_dims.append([1] * max_rank)
                elif len(in_shape) == max_rank:
                    reshape_dims.append(list(in_shape))
                elif i in new_dims_dict:
                    reshape_dims.append(new_dims_dict[i])
                else:
                    WARN('[Parser]: Meets error when calculating broadcast!')
                    break

            max_dims = np.max(np.array([list(s) for s in reshape_dims if len(
                s) == max_rank]), axis=0, keepdims=False).tolist()
            reps = [(np.array(max_dims) // np.array(dim)).tolist()
                    for dim in reshape_dims]

            for i, s in enumerate(input_shapes):
                meta_ret = {'reshape': None, 'tile': None}
                if list(s) != reshape_dims[i]:
                    meta_ret.update({'reshape': reshape_dims[i]})
                if any([r != 1 for r in reps[i]]):
                    meta_ret.update({'tile': reps[i]})
                ret.append(meta_ret)
        else:
            WARN(
                '[Parser]: Only broadcast when inputs number greater or equal to 2 in cal_reshape_and_tile!')
        return ret

    @staticmethod
    def broad_cast(inputs):
        '''This function returns the output after brocast.'''
        ret = copy.deepcopy(inputs)
        if len(inputs) >= 2:
            input_shapes = [s.shape for s in inputs]
            reshape_tile = OpNeedBroadcast.cal_reshape_and_tile(input_shapes)
            if len(reshape_tile) == len(ret):
                for i, inp in enumerate(zip(ret, reshape_tile)):
                    value, params = inp
                    if params.get('reshape', None) is not None:
                        value = np.reshape(value, params['reshape'])
                    if params.get('tile', None) is not None:
                        value = np.tile(value, params['tile'])
                    ret[i] = value
            else:
                WARN(
                    '[Parser]: Number of broadcast params shoud be equal to number of inputs!')
        return ret

    @classmethod
    def attributes(cls):
        '''return attributes of OpNeedBroadcast class.'''
        return {'broad_casted': {'type': AttrType.TENSORS, 'default': []}}

    @abc.abstractmethod
    def infer_shape(self):
        '''An abstract method for shape inference.'''
        super(OpNeedBroadcast, self).infer_shape()
        inputs = self.get_input_tensors()
        self.broad_casted = OpNeedBroadcast.broad_cast(inputs)


class OpNeedUniBroadcast(Op):
    '''
    Class OpNeedBroadcast inherited from Op class.
    Unidirectional broadcast means that input1 can broadcast to input2, but input2 cannot brocast to input1.
    Ops need unidirectional brocast must inherit ConstLikeOp.
    '''
    @staticmethod
    def cal_reshape_and_tile(input_shapes):
        '''We implement brocast by adding reshape and tile, and this function calculates the parameters of reshape and tile.'''
        ret = []
        if len(input_shapes) >= 2:
            max_rank = max(len(input_shapes[0]), 1)
            max_dims_array = np.array(
                [list(s) for s in input_shapes[0:1] if len(s) == max_rank])
            if max_dims_array.size > 0:
                max_dims = np.max(max_dims_array, axis=0,
                                  keepdims=False).tolist()
            else:
                max_dims = []

            reshape_dims, reps = [], []
            for i, in_shape in enumerate(input_shapes[1:]):
                if len(in_shape) == 0:
                    reshape_dims.append([1] * max_rank)
                elif len(in_shape) == max_rank:
                    reshape_dims.append(list(in_shape))
                else:
                    found_index = -1
                    cur_index = 0
                    for si, s in enumerate(in_shape):
                        if s == 1:
                            continue
                        else:
                            if s in max_dims:
                                index = max_dims.index(s)
                                found_index = index
                                cur_index = si
                                break
                    if found_index != -1:
                        new_dim = [1] * (found_index - cur_index) + list(in_shape) + [1] * (
                            max_rank - len(in_shape) - (found_index - cur_index))
                    else:
                        new_dim = list(in_shape) + \
                            [1] * (max_rank - len(in_shape))
                    if len(new_dim) > max_rank:
                        WARN('[Parser]: Meets error when calculating broadcast!')
                    reshape_dims.append(new_dim)

            reps = [(np.array(max_dims) // np.array(dim)).tolist()
                    for dim in reshape_dims]

            for i, s in enumerate(input_shapes[1:]):
                meta_ret = {'reshape': None, 'tile': None}
                if list(s) != reshape_dims[i]:
                    meta_ret.update({'reshape': reshape_dims[i]})
                if any([r != 1 for r in reps[i]]):
                    meta_ret.update({'tile': reps[i]})
                ret.append(meta_ret)
        else:
            WARN('[Parser]: Only broadcast when inputs number greater or equal to 2!')
        return ret

    @staticmethod
    def broad_cast(inputs):
        '''This function returns the output after brocast.'''
        ret = copy.deepcopy(inputs)
        if len(inputs) >= 2:
            input_shapes = [s.shape for s in inputs]
            reshape_tile = OpNeedUniBroadcast.cal_reshape_and_tile(
                input_shapes)
            if len(reshape_tile) == len(ret) - 1:
                for i, inp in enumerate(zip(ret[1:], reshape_tile)):
                    value, params = inp
                    if params.get('reshape', None) is not None:
                        value = np.reshape(value, params['reshape'])
                    if params.get('tile', None) is not None:
                        value = np.tile(value, params['tile'])
                    ret[i+1] = value
            else:
                WARN(
                    '[Parser]: Number of broadcast params shoud be equal to number of inputs!')
        return ret


class InputLikeOp(Op):
    '''
    Class InputLikeOp inherited from Op class.
    This class is used to describe a special input op.
    '''
    @abc.abstractmethod
    def infer_shape(self, input_tensor=None):
        '''An abstract method for shape inference.'''
        pass


class OnnxOp(Op):
    '''
    Class OnnxOp inherited from Op class.
    All ops under the onnx framework must inherit this class.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of OnnxOp class.'''
        return {'opset_version': {'type': AttrType.INT, 'default': 1, 'required': True}}

    @classmethod
    def max_ver(cls):
        '''Return the maximum version of this onnx op.'''
        return max(list(cls.attributes().keys()))

    @classmethod
    def cal_ver(cls, opset_version):
        '''Calculate the version of the onnx op.'''
        if opset_version in cls.attributes().keys():
            return opset_version
        else:
            ver_list = [k for k in cls.attributes().keys()]
            mask = np.where(np.array(ver_list, np.int64) <= opset_version)[0]
            if mask.size == 0:
                ERROR('[Parser]: All history versions is greater than opset version for %s!' % (
                    type(cls).__name__))
            else:
                return ver_list[mask[-1]]

    def __init__(self, graph, attr_dict=None):
        super(OnnxOp, self).__init__(graph, attr_dict)
        if 'opset_version' not in attr_dict:
            attr_dict['opset_version'] = type(self).max_ver()
        self.update_attributes(OnnxOp, attr_dict)
        self._attr['cur_version'].value = self.cal_ver(
            self._attr['opset_version'].value)

    def convert_version(self):
        '''Virtual function for convert version.'''
        pass


class BaseOnnxPoolOp(OpHasPaddingStrides, OnnxOp):
    '''
    Class BaseOnnxPoolOp inherited from OpHasPaddingStrides,OnnxOp class.
    All pooling layers under the onnx framework must inherit this class.
    '''
    @staticmethod
    def cal_out_shape(in_shape, pads, strides, kernel_shape, auto_pad, dilations=None, ceil_mode=False):
        '''Calculate output shape according to in_shape, pads, strides, kernel_shape, auto_pad and other parameters.'''
        assert auto_pad in ('NOTSET', 'VALID', 'SAME_UPPER',
                            'SAME_LOWER'), 'The value of auto_pad is invalid in BaseOnnxPoolOp.'
        if dilations is None:
            dilations = [1] * len(kernel_shape)
        params = [in_shape, strides, kernel_shape, dilations]
        in_shape, strides, kernel_shape, dilations = [
            np.array(p, np.int64) for p in params]
        effective_kernel_shape = (kernel_shape - 1) * dilations + 1
        padding = np.reshape(np.array(pads, np.int64), (2, -1))
        padding = np.sum(padding, axis=0, keepdims=False)
        if auto_pad == 'NOTSET':
            if ceil_mode:
                out_shape = np.ceil(
                    (in_shape + padding - effective_kernel_shape) / strides + 1)
            else:
                out_shape = np.floor(
                    (in_shape + padding - effective_kernel_shape) / strides + 1)
        elif auto_pad == 'VALID':
            out_shape = np.ceil(
                (in_shape - effective_kernel_shape + 1) / strides)
        else:
            out_shape = np.ceil(in_shape / strides)
        return out_shape.astype(np.int64).tolist()

    @classmethod
    def attributes(cls):
        '''return attributes of BaseOnnxPoolOp class.'''
        return {'kernel_shape': {'required': True},
                'count_include_pad': {'type': AttrType.INT, 'default': 0},
                'ceil_mode': {'type': AttrType.INT, 'default': 0}
                }

    def __init__(self, graph, attr_dict=None):
        super(BaseOnnxPoolOp, self).__init__(graph, attr_dict)
        self.update_attributes(BaseOnnxPoolOp, attr_dict)
        self.check_required()

    def __getattr__(self, item):
        '''Returns the OP object property value.'''
        ret = None
        try:
            if item in ('ceil_mode', 'count_include_pad'):
                ret = self.__dict__['_attr'][item].value
                ret = False if ret is None else bool(ret)
        except:
            ret = None
        if ret is None:
            ret = super(BaseOnnxPoolOp, self).__getattr__(item)
        return ret


class TfliteOp(Op):
    '''
    Class TfliteOp inherited from Op class.
    All ops under the tflite framework must inherit TfliteOp.
    '''
    @staticmethod
    def cal_fused_activations(inputs, fused_activations):
        '''Returns different activations functions according to the fused_activations parameter.'''
        assert fused_activations in ['NONE', 'RELU', 'RELU_N1_TO_1', 'RELU6', 'TANH',
                                     'SIGN_BIT'], ('Meets invalid TFLite activation type (%s)' % fused_activations)
        ret = inputs
        if fused_activations == 'RELU':
            ret = np.maximum(inputs, 0.)
        elif fused_activations == 'RELU_N1_TO_1':
            ret = np.clip(inputs, -1., 1.)
        elif fused_activations == 'RELU6':
            ret = np.clip(inputs, 0., 6.)
        elif fused_activations == 'TANH':
            ret = np.tanh(inputs)
        elif fused_activations == 'SIGN_BIT':
            WARN('[Parser]: Dose not support TFLite Activation type SIGN_BIT yet!')
        return ret

    @classmethod
    def attributes(cls):
        '''return attributes of TfliteOp class.'''
        return {'opcode_version': {'type': AttrType.INT, 'default': 1, 'required': True}}

    def __init__(self, graph, attr_dict=None):
        super(TfliteOp, self).__init__(graph, attr_dict)
        self.update_attributes(TfliteOp, attr_dict)
        if self.opcode_version not in type(self).attributes():
            WARN('[Parser]: Node(%s) Opcode Version %d is not implemented for %s! But will try to proceed.' % (
                self.name, self.opcode_version, type(self).__name__))

    @property
    def opcode_version(self):
        '''Get the content of opcode_version.'''
        attr = self._attr.get('opcode_version', None)
        return attr.value if attr is not None else 1

    @property
    def correspond_onnx_op(self):
        return None


class TfliteReduceOp(OpHasAxis, OpHasOneOutPort, TfliteOp):
    '''
    Class TfliteReduceOp inherited from OpHasAxis, OpHasOneOutPort, TfliteOp class.
    All reduce ops under the tflite framework must inherit TfliteReduceOp.
    '''
    @classmethod
    def ufunc(cls):
        return None

    @classmethod
    def attributes(cls):
        '''return attributes of TfliteReduceOp class.'''
        return {1: {}, 2: {}}

    def __init__(self, graph, attr_dict=None):
        super(TfliteReduceOp, self).__init__(graph, attr_dict)
        self.update_attributes(TfliteReduceOp, attr_dict)
        assert self.check_required(), 'TfliteReduceOp is missing a required parameter.'

    def __getattr__(self, item):
        '''Returns the OP object property value.'''
        if item == 'axes':
            try:
                inputs = self.get_input_tensors()
                ret = np.array(inputs[1]).tolist()
                if not isinstance(ret, list):
                    ret = [ret]
                self.__dict__['_attr'][item] = Attribute(
                    item, {'type': AttrType.INTS, 'value': ret})
            except:
                ret = None
        else:
            ret = super(TfliteReduceOp, self).__getattr__(item)
        return ret

    @abc.abstractmethod
    def infer_shape(self):
        '''An abstract method for shape inference.'''
        super(TfliteReduceOp, self).infer_shape()
        inputs = self.get_input_tensors()
        out_tensor = type(self).ufunc()(
            inputs[0], axis=tuple(self.axes), keepdims=self.keepdims)
        self.set_out_tensor(out_tensor)


class CaffeOp(Op):
    '''
    Class CaffeOp inherited from Op class.
    All ops under the caffe framework must inherit CaffeOp.
    '''

    def __init__(self, graph, attr_dict=None):
        super(CaffeOp, self).__init__(graph, attr_dict)
        self.update_attributes(CaffeOp, attr_dict)

    @property
    def correspond_onnx_op(self):
        return None


class CaffeHasPad(OpHasPaddingStrides, CaffeOp):
    '''
    Class CaffeHasPad inherited from OpHasPaddingStrides, CaffeOp class.
    All ops which has pad under the caffe framework must inherit CaffeHasPad.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of CaffeHasPad class.'''
        return {'pad_h': {'type': AttrType.INT, 'default': 0},
                'pad_w': {'type': AttrType.INT, 'default': 0},
                'kernel_h': {'type': AttrType.INT, 'default': None},
                'kernel_w': {'type': AttrType.INT, 'default': None},
                'stride_h': {'type': AttrType.INT, 'default': 1},
                'stride_w': {'type': AttrType.INT, 'default': 1},
                }

    def __init__(self, graph, attr_dict=None):
        super(CaffeHasPad, self).__init__(graph, attr_dict)
        self.update_attributes(CaffeHasPad, attr_dict)
        assert self.check_required(), 'CaffeHasPad is missing a required parameter.'

    def __getattr__(self, item):
        '''Returns the OP object property value.'''
        ret = None
        try:
            if item == 'kernel_shape':
                ret = self.__dict__['_attr'][item].value
                kernel_h = self.__dict__['_attr']['kernel_h'].value
                kernel_w = self.__dict__['_attr']['kernel_w'].value
                if ret is None and kernel_h is not None and kernel_w is not None:
                    ret = [kernel_h, kernel_w]
                    self.__dict__['_attr'][item].value = ret
            elif item == 'pads':
                ret = self.__dict__['_attr'][item].value
                if ret is None:
                    pad_h = self.__dict__['_attr']['pad_h'].value
                    pad_w = self.__dict__['_attr']['pad_w'].value
                    ret = np.array([pad_h, pad_w, pad_h, pad_w],
                                   np.int64).tolist()
                    self.__dict__['_attr'][item].value = ret
            elif item == 'strides':
                ret = self.__dict__['_attr'][item].value
                if ret is None:
                    ret = [self.__dict__['_attr']['stride_h'].value,
                           self.__dict__['_attr']['stride_w'].value]
                    self.__dict__['_attr'][item].value = ret
        except:
            ret = None
        if ret is None:
            ret = super(CaffeHasPad, self).__getattr__(item)
        return ret


class CaffeHasBaseScaleShift(CaffeOp):
    '''
    Class CaffeHasBaseScaleShift inherited from CaffeOp class.
    All ops which has scale and shift under the caffe framework must inherit CaffeHasBaseScaleShift.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of CaffeHasBaseScaleShift class.'''
        return {'base': {'type': AttrType.FLOAT, 'default': -1.0},
                'scale': {'type': AttrType.FLOAT, 'default': 1.0},
                'shift': {'type': AttrType.FLOAT, 'default': 0.0}
                }

    def __init__(self, graph, attr_dict=None):
        super(CaffeHasBaseScaleShift, self).__init__(graph, attr_dict)
        self.update_attributes(CaffeHasBaseScaleShift, attr_dict)
        assert self.check_required(), 'CaffeHasBaseScaleShift is missing a required parameter.'

    @property
    def base(self):
        '''Get the value of the parameter base.'''
        return float(self.get_attr_by_key('base'))

    @property
    def scale(self):
        '''Get the value of the parameter scale.'''
        return float(self.get_attr_by_key('scale'))

    @property
    def shift(self):
        '''Get the value of the parameter shift.'''
        return float(self.get_attr_by_key('shift'))


class CaffeHasBiasTerm(CaffeOp):
    '''
    Class CaffeHasBiasTerm inherited from CaffeOp class.
    All ops which has bias_term under the caffe framework must inherit CaffeHasBaseScaleShift.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of CaffeHasBiasTerm class.'''
        return {'bias_term': {'type': AttrType.INT, 'default': 1}
                }

    def __init__(self, graph, attr_dict=None):
        super(CaffeHasBiasTerm, self).__init__(graph, attr_dict)
        self.update_attributes(CaffeHasBiasTerm, attr_dict)
        assert self.check_required(), 'CaffeHasBiasTerm is missing a required parameter.'

    @property
    def bias_term(self):
        '''Get the value of the parameter bias_term.'''
        return bool(self.get_attr_by_key('bias_term'))


class CaffeRecurrent(OpHasVariableOutPorts, CaffeOp):
    '''
    Class CaffeRecurrent inherited from OpHasVariableOutPorts, CaffeOp class.
    All recurrent ops under the caffe framework must inherit CaffeRecurrent,similar to LSTM.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of CaffeRecurrent class.'''
        return {'expose_hidden': {'type': AttrType.INT, 'default': 0},
                'num_output': {'type': AttrType.INT, 'default': 0},
                'weights_list': {'type': AttrType.TENSORS, 'default': []}
                }

    def __init__(self, graph, attr_dict=None):
        super(CaffeRecurrent, self).__init__(graph, attr_dict)
        self.update_attributes(CaffeRecurrent, attr_dict)
        assert self.check_required(), 'CaffeRecurrent is missing a required parameter.'

    @property
    def expose_hidden(self):
        '''Get the value of the parameter expose_hidden.'''
        return bool(self.get_attr_by_key('expose_hidden'))

    @property
    def num_output(self):
        '''Get the value of the parameter num_output.'''
        return self.get_attr_by_key('num_output')

    @property
    def weights_list(self):
        '''Get the value of the parameter weights_list.'''
        return self.get_attr_by_key('weights_list')

    @weights_list.setter
    def weights_list(self, value):
        '''Set the value of the parameter weights_list.'''
        self._attr['weights_list'].value = list(value)


class CommonOp(Op):
    '''
    Class CommonOp inherited from Op class.
    Ops that do not appear in the ONNX documentation need to inherit the common op.
    '''
    pass


class TfOp(Op):
    '''
    Class TfOp inherited from Op class.
    All ops under the Tensorflow framework must inherit TfOp.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of TfOp class.'''
        return {'data_format': {'default': 'NHWC'},
                'dtype': {'type': AttrType.STRING, 'required': False, 'default': 'float32'},
                'Tidx': {'type': AttrType.STRING,
                         'default': 'int32',
                         'options': ['int8', 'int16', 'int32', 'int64',
                                     'uint16', 'uint32',
                                     'bfloat16', 'half', 'float32', 'float64', 'float', 'double']},
                'opcode_version': {'type': AttrType.INT, 'required': False, 'default': 1},
                }

    def __init__(self, graph, attr_dict=None):
        super(TfOp, self).__init__(graph, attr_dict)
        self.update_attributes(TfOp, attr_dict)
        assert self.check_required(), 'TfOp is missing a required parameter.'

    @property
    def correspond_onnx_op(self):
        return None

    @property
    def dtype(self):
        '''Get the value of the parameter dtype.'''
        return self.get_attr_by_key('dtype')


class TfHasN(TfOp):
    '''
    Class TfHasN inherited from TfOp class.
    All ops which has N under the Tensorflow framework must inherit TfHasN.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of TfHasN class.'''
        return {'N': {'type': AttrType.INT, 'required': False}}

    def __init__(self, graph, attr_dict=None):
        super(TfHasN, self).__init__(graph, attr_dict)
        self.update_attributes(TfHasN, attr_dict)
        assert self.check_required(), 'TfHasN is missing a required parameter.'


class TfHasPaddingStrides(OpHasPaddingStrides, TfOp):

    @abc.abstractmethod
    def infer_shape(self):
        ''' An abstract method for shape inference. '''
        super(TfHasPaddingStrides, self).infer_shape()
        inputs = self.get_input_tensors()
        if (len(inputs) >= 1
            and inputs[0] is not None
            and len(inputs[0].shape) >= 3) \
                or (self.type in ('TfConv2DBackpropInput', 'TfConv3DBackpropInputV2')
                    and len(inputs) >= 2
                    and inputs[1] is not None
                    and len(inputs[1].shape) in (4, 5)):
            if self.type in ('TfConv2DBackpropInput', 'TfConv3DBackpropInputV2'):
                spatial_len = len(inputs[1].shape) - 2
            else:
                spatial_len = len(inputs[0].shape) - 2

            if len(self.strides) in (1, spatial_len, spatial_len + 2):
                if len(self.strides) == 1:
                    self.strides = self.strides * spatial_len
                elif len(self.strides) == spatial_len + 2:
                    if len(self.data_format) >= 3 and self.data_format[:2] == 'NC' \
                            and self.strides[0] == 1 and self.strides[1] == 1:
                        self.strides = self.strides[2:]
                    elif (not self.data_format
                          or (len(self.data_format) >= 3
                              and self.data_format[0] == 'N'
                              and self.data_format[-1] == 'C')) \
                            and self.strides[0] == 1 \
                            and self.strides[-1] == 1:
                        self.strides = self.strides[1:-1]
                    else:
                        WARN(
                            '[Parser]: Meets invalid strides for Node(%s) in infer_shape!' % self.name)
            else:
                WARN(
                    '[Parser]: Meets invalid strides for Node(%s) in infer_shape!' % self.name)

            if len(self.dilations) in (1, spatial_len, spatial_len + 2):
                if len(self.dilations) == 1:
                    self.dilations = self.dilations * spatial_len
                elif len(self.dilations) == spatial_len + 2:
                    if len(self.data_format) >= 3 \
                            and self.data_format[:2] == 'NC' \
                            and self.dilations[0] == 1 \
                            and self.dilations[1] == 1:
                        self.dilations = self.dilations[2:]
                    elif (not self.data_format
                          or (len(self.data_format) >= 3
                              and self.data_format[0] == 'N'
                              and self.data_format[-1] == 'C')) \
                            and self.dilations[0] == 1 \
                            and self.dilations[-1] == 1:
                        self.dilations = self.dilations[1:-1]
                    else:
                        WARN(
                            '[Parser]: Meets invalid dilations for Node(%s) in infer_shape!' % self.name)
            else:
                WARN(
                    '[Parser]: Meets invalid dilations for Node(%s) in infer_shape!' % self.name)

            if not isinstance(self, OpHasWeights):
                if len(self.kernel_shape) in (1, spatial_len, spatial_len + 2):
                    if len(self.kernel_shape) == 1:
                        self.kernel_shape = self.kernel_shape * spatial_len
                    elif len(self.kernel_shape) == spatial_len + 2:
                        if self.data_format[:2] == 'NC' and self.kernel_shape[0] == 1 and self.kernel_shape[1] == 1:
                            self.kernel_shape = self.kernel_shape[2:]
                        elif (not self.data_format or (self.data_format[0] == 'N' and self.data_format[-1] == 'C')) and \
                                self.kernel_shape[0] == 1 and self.kernel_shape[-1] == 1:
                            self.kernel_shape = self.kernel_shape[1:-1]
                        else:
                            WARN(
                                '[Parser]: Meets invalid kernel_shape for Node(%s) in infer_shape!' % self.name)
                else:
                    WARN(
                        '[Parser]: Meets invalid kernel_shape for Node(%s) in infer_shape!' % self.name)


class TfRecurrent(OpHasVariableOutPorts, TfOp):
    '''
    Class TfRecurrent inherited from OpHasVariableOutPorts, TfOp class.
    All recurrent ops(GRU, LSTM and etc) under the tf framework must inherit TfRecurrent.
    '''
    @classmethod
    def attributes(cls):
        '''return attributes of TfRecurrent class.'''
        return {'units': {'type': AttrType.INT, 'required': True},
                'activation': {'type': AttrType.STRING, 'required': False, 'default': 'tanh',
                               'options': ['elu', 'exponential', 'gelu', 'hard_sigmoid',
                                           'linear', 'relu', 'selu', 'sigmoid', 'softmax',
                                           'softplus', 'softsign', 'swish', 'tanh']},
                'recurrent_activation': {'type': AttrType.STRING, 'required': False, 'default': 'sigmoid',
                                         'options': ['elu', 'exponential', 'gelu', 'hard_sigmoid',
                                                     'linear', 'relu', 'selu', 'sigmoid', 'softmax',
                                                     'softplus', 'softsign', 'swish', 'tanh']},
                'use_bias': {'type': AttrType.INT, 'required': False, 'default': 1, 'options': [0, 1]},
                'return_sequences': {'type': AttrType.INT, 'required': False, 'default': 0, 'options': [0, 1]},
                'return_state': {'type': AttrType.INT, 'required': False, 'default': 0, 'options': [0, 1]},
                'go_backwards': {'type': AttrType.INT, 'required': False, 'default': 0, 'options': [0, 1]},
                'time_major': {'type': AttrType.INT, 'required': False, 'default': 0, 'options': [0, 1]},
                'weights_list': {'type': AttrType.TENSORS, 'default': []}
                }

    def __init__(self, graph, attr_dict=None):
        super(TfRecurrent, self).__init__(graph, attr_dict)
        self.update_attributes(TfRecurrent, attr_dict)
        assert self.check_required(), 'TfRecurrent is missing a required parameter.'

    def __getattr__(self, item):
        ret = None
        try:
            if item in ('use_bias', 'return_sequences', 'return_state', 'go_backwards', 'time_major', ):
                ret = bool(self.__dict__['_attr'][item].value)
        except:
            ret = None
        if ret is None:
            ret = super(TfRecurrent, self).__getattr__(item)
        return ret


class ArmOp(Op):
    '''
    Class ArmOp inherited from Op class.
    All Ops contained in IR def must inherit Armop
    '''
    @classmethod
    def num_in_ports(cls):
        return 1
