# Copyright © 2022 Arm Technology (China) Co. Ltd. All rights reserved.
# SPDX-License-Identifier: Apache-2.0


from .load import convert_tflite_to_graph
from .passes.front_passes import split_op_has_activation, split_fc, split_s2b, split_b2s, split_greater_or_less_equal, \
    split_not_equal, split_rsqrt, remove_detection_postprocess, convert_to_onnx, convert_onehot, convert_reverse_sequence, convert_square, \
    convert_unpack, convert_negative_pool_pad, convert_scatternd, convert_special_uni_seq_lstm, convert_strided_slice, convert_square_diff, \
    convert_broadcast_to, remove_redundant_broadcast_to, remove_sub_equal_select, remove_dequantize
from ..onnx.passes.front_passes import fuse_weights_const
from ..onnx.passes.common_passes import apply_subgraph_plugin, record_output_tensors
from ...graph.graph_algo import infer, clear_redundant_nodes
from ...logger import INFO, DEBUG, WARN, ERROR, FATAL


def process_tflite(model_path, params):
    '''Do some preprocessing on the graph under the tflite framework.'''
    graph = convert_tflite_to_graph(model_path, params)
    record_output_tensors(graph)
    if graph is not None and len(graph) > 0:
        apply_subgraph_plugin(graph)
        infer(graph, partial=True)
        fuse_weights_const(graph)

        split_op_has_activation(graph)
        split_fc(graph)
        split_s2b(graph)
        split_b2s(graph)
        split_greater_or_less_equal(graph)
        split_not_equal(graph, 'LiteNOT_EQUAL')
        split_rsqrt(graph)

        from ..tf.passes.front_passes import split_special_floormod
        split_special_floormod(graph, 'LiteFLOOR_MOD')

        remove_dequantize(graph)
        remove_detection_postprocess(graph)
        convert_onehot(graph)

        convert_square(graph, 'LiteSQUARE')
        convert_square_diff(graph, 'LiteSQUARED_DIFFERENCE')
        convert_scatternd(graph, 'LiteSCATTER_ND')
        convert_reverse_sequence(graph, 'LiteREVERSE_SEQUENCE')
        convert_unpack(graph)
        convert_special_uni_seq_lstm(graph)

        convert_strided_slice(graph, 'LiteSTRIDED_SLICE')

        clear_redundant_nodes(graph)
        infer(graph)
        convert_negative_pool_pad(graph)
        remove_redundant_broadcast_to(graph)
        convert_broadcast_to(graph)
        remove_sub_equal_select(graph)

        from ..tf.passes.front_passes import convert_nms
        convert_nms(graph, params)

        convert_to_onnx(graph)
    else:
        WARN('[Parser]: Got empty graph in process_tflite!')
    return graph
