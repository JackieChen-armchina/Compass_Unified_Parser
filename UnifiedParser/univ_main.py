# Copyright © 2022 Arm Technology (China) Co. Ltd. All rights reserved.
# SPDX-License-Identifier: Apache-2.0


import onnx
import torch
import tensorflow.compat.v1 as tf
from collections import OrderedDict
from .common.utils import is_file, is_dir, multi_string_to_list, list_string_to_list
from .logger import *
from .front_end.onnx.process import process_onnx
from .front_end.onnx.passes.middle_passes import middle_passes, convert_onnx_version
from .front_end.onnx.passes.back_passes import back_passes, trim_weights
from .front_end.onnx.passes.transform import transform_to_nhwc
from .front_end.lite.process import process_tflite
from .front_end.caffe.process import process_caffe
from .front_end.onnx.passes.common_passes import remove_useless_op
from .front_end.tf.process import process_tf
from .front_end.torch.process import convert_torch_to_onnx
from .graph.graph_algo import infer, has_path
from .graph.pattern_match import matched_patterns, single_node_matcher
from .writer import serialize
from .preprocess import gamut_preprocess, preprocess
from .misc import special_character_conversion


def univ_parser(params):
    ret = True

    if params:
        '''Set the necessary parameters.'''
        model_path = params.get('input_model', '')
        output_dir = params.get('output_dir', './')
        model_type = params.get('model_type', '')
        if 'input_names' not in params and 'input' in params:
            params['input_names'] = params['input']
            params.pop('input')
        if 'output_names' not in params and 'output' in params:
            params['output_names'] = params['output']
            params.pop('output')
        if 'input_shapes' not in params and 'input_shape' in params:
            params['input_shapes'] = params['input_shape']
            params.pop('input_shape')

        params['input_names'] = multi_string_to_list(
            params['input_names']) if 'input_names' in params else []
        params['output_names'] = multi_string_to_list(
            params['output_names']) if 'output_names' in params else []
        out_names_dict = OrderedDict(
            {k: i for (i, k) in enumerate(params['output_names'])})
        params['output_names'] = list(out_names_dict.keys())
        params['input_shapes'] = list_string_to_list(
            params['input_shapes']) if 'input_shapes' in params else []

        if model_type == 'torch':
            # For torch, input_names and output_names are useless because it's not allowed to change
            # input nodes or output nodes for TorchScript. They are just names assigned to the input
            # and output nodes of the graph in order. So, only providing input_shapes is allowed.
            # If input_names is not set, set it to ['x0', 'x1', ...].
            if not params['input_shapes']:
                FATAL('[Parser]: input_shapes must be provided in config file for torch model!')
            if params['input_names'] or params['output_names']:
                INFO('[Parser]: input_names and output_names in config file won\'t change input '
                     'and output nodes for torch model!')
            if not params['input_names']:
                params['input_names'] = [('x' + str(idx)) for idx in range(len(params['input_shapes']))]

        if len(params['input_names']) == len(params['input_shapes']):
            params['input_shapes'] = {
                params['input_names'][i]: v for i, v in enumerate(params['input_shapes'])}
        else:
            FATAL(
                '[Parser]: Length of input_names should be equal to length of input_shapes! '
                'Please check config file!')

        if 'batch_size' in params:
            WARN(
                '[Parser]: batch_size in config file will be deprecated and has no effect!')
        if 'input_data_format' in params:
            WARN('[Parser]: input_data_format in config file will be deprecated!')
        params['input_data_format'] = 'NCHW' if model_type in ('onnx', 'caffe', 'torch') else 'NHWC'
        params['output_tensor_names'] = params['output_names'][:]

        if (is_file(model_path) or is_dir(model_path)) and is_dir(output_dir):
            graph = None

            if int(tf.__version__.split('.')[0]) >= 2:
                tf.disable_eager_execution()

            try:
                # Convert torch model to onnx before processing
                if model_type == 'torch':
                    model_path, params = convert_torch_to_onnx(model_path, params)
                    model_type = 'onnx'

                '''The models under different frameworks are parsed and finally converted into representations under the onnx framework.'''
                if model_type == 'onnx':
                    graph = process_onnx(model_path, params)
                elif model_type == 'tflite':
                    graph = process_tflite(model_path, params)
                elif model_type == 'caffe':
                    graph = process_caffe(model_path, params)
                elif model_type in ('tf', 'tensorflow'):
                    graph = process_tf(model_path, params)
                else:
                    ERROR('[Parser]: Framework %s is not supported!' %
                          params.get('model_type', ''))
            except Exception as e:
                ERROR('[Parser]: Meets error when processing models, %s!' % str(e))
                ret = False

            if graph:
                '''Check if it is a connected graph.'''
                input_names = []
                input_names_list = single_node_matcher(graph, 'Input')
                for input_name in input_names_list:
                    input_names.append(input_name['target'])
                output_names = graph._attr.get('output_names')
                for output_name in output_names:
                    has_path_flag = False
                    for input_name in input_names:
                        if has_path(graph, input_name, output_name):
                            has_path_flag = True
                            break
                    if has_path_flag is False:
                        ERROR('[Parser]: Graph is not a connected one!')

                '''Gives a 'may be time consuming' hint for huge models.'''
                if len(graph) >= 2000:
                    WARN(
                        '[Parser]: Begin to process large model (number of nodes = %d) and maybe cost quite a lot of time!' % len(graph))

                try:
                    preprocess(graph, params)
                except Exception as e:
                    WARN(
                        '[Parser]: Meets exception in insert preprocess (%s)!' % str(e))

                try:
                    convert_onnx_version(graph)
                except Exception as e:
                    WARN(
                        '[Parser]: Meets exception in convert_onnx_version (%s)!' % str(e))

                try:
                    middle_passes(graph, params)
                except Exception as e:
                    WARN('[Parser]: Meets exception in middle_passes (%s)!' % str(e))

                infer(graph)

                try:
                    transform_to_nhwc(graph, params)
                except Exception as e:
                    WARN(
                        '[Parser]: Meets exception in transform_to_nhwc (%s)!' % str(e))

                try:
                    back_passes(graph, params)
                except Exception as e:
                    WARN('[Parser]: Meets exception in back_passes (%s)!' % str(e))

                try:
                    gamut_preprocess(graph, params)
                except Exception as e:
                    WARN(
                        '[Parser]: Meets exception in insert gamut preprocess (%s)!' % str(e))

                try:
                    special_character_conversion(graph, params)
                except Exception as e:
                    WARN(
                        '[Parser]: Meets exception in insert special character conversion (%s)!' % str(e))

                try:
                    infer(graph)
                    remove_useless_op(graph, ['ArmCast'])
                except Exception as e:
                    ERROR('[Parser]: Meets exception in last infer (%s)!' % str(e))

                try:
                    trim_weights(graph)
                    serialize(graph, params)
                except Exception as e:
                    ERROR('[Parser]: Meets exception in serialize (%s)!' % str(e))

            else:
                WARN('[Parser]: Got invalid or empty graph from model!')
                ret = True
        else:
            FATAL('[Parser]: Meets invalid model file or invalid output directory!')
            ret = False
    else:
        ERROR('[Parser]: Meets invalid parameters for universal parser!')
        ret = False
    return ret


def main():
    import sys
    import argparse
    import configparser
    from .logger import get_error_count

    args = argparse.ArgumentParser()
    args.add_argument('-c', '--cfg', metavar='<net.cfg>',
                      type=str, required=True, help='graph configure file.')
    args.add_argument('-l', '--log', metavar='<net.log>',
                      type=str, required=False, default=None, help='redirect parser output to log file.')
    args.add_argument('-v', '--verbose',
                      required=False, default=False, action='store_true', help='verbose output.')

    options = args.parse_args(sys.argv[1:])
    logfile = options.log
    verbose = options.verbose
    init_logging(verbose, logfile)

    exit_code = 0

    if options.cfg and len(options.cfg) != 0:
        config = configparser.ConfigParser()
        try:
            config.read(options.cfg)
        except configparser.MissingSectionHeaderError as e:
            FATAL('Config file error: %s' % (str(e)))

        if 'Common' in config:
            common = config['Common']
            model_type = 'tensorflow'
            if 'model_type' in common:
                model_type = common['model_type']
                if model_type.upper() not in ('ONNX', 'TFLITE', 'CAFFE', 'TENSORFLOW', 'TF', 'TORCH'):
                    WARN('Unsupport model type!')
                    return -1

            model_type = model_type.lower()
            common['model_type'] = model_type

            INFO('Begin to parse %s model %s...' %
                 (model_type, common.get('model_name', '')))

            param = dict(common)
            meta_ret = univ_parser(param)
            if not meta_ret:
                exit_code = -1
                ERROR('Universal parser meets error!')

            if get_error_count() > 0:
                exit_code = -1
                WARN('Parser Failed!')
            else:
                INFO('Parser done!')
        else:
            exit_code = -1
            WARN('Common section is required in config file.')

    return exit_code
