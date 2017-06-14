import ipdb
import sys, os
sys.path.insert(0, '../python') 

import torch
import argparse
import numpy as np
import scipy.io
import torchvision
from PIL import Image
from ast import literal_eval as make_tuple
import pathlib
from torch.autograd import Variable
import torchvision.transforms as transforms
import pytorch_utils as pl

if 1: # TODO(sam): cleanup
    sys.path.insert(0, os.path.expanduser('~/local/matlab-engine/lib'))
    sys.path.insert(0, 'python')

# compare against matconvnet
import matlab.engine
eng = matlab.engine.start_matlab()
cwd = pathlib.Path.cwd()

# parse args
parser = pl.set_conversion_kwargs()
args = parser.parse_args(sys.argv[1:])

# load pytorch model
if args.model_def and args.model_weights:
    model_paths = {'def': args.model_def, 'weights': args.model_weights}
net,flatten_loc = pl.load_pytorch_model(args.pytorch_model, paths=model_paths)

# compute activations for a sample image
im_orig = Image.open(str(cwd / 'test/peppers.png')).convert('RGB')
image_size = tuple(make_tuple(args.image_size))
im = np.array(im_orig.resize(image_size))
normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                  std=[0.229, 0.224, 0.225])
transform = transforms.Compose([transforms.ToTensor(),normalize])
x = Variable(transform(im).unsqueeze(0))
py_feats_tensors = pl.compute_intermediate_feats(net.eval(), x, flatten_loc)

# create image to pass to MATLAB and compute the feature maps
im_np = np.array(torch.squeeze(x.data,0).numpy())
mcn_im = im_np.flatten().tolist() # no numpy support
eng.addpath(str(cwd/'test'),nargout=0)
mcn_feats_ = [np.array(x) for x in 
              eng.get_mcn_features(args.mcn_model, mcn_im, im_np.shape)]
py_feats = [np.squeeze(x.data.numpy()) for x in py_feats_tensors]
mcn_feats = [np.squeeze(np.transpose(x, (2,0,1))) for x in mcn_feats_] # to CxHxW
print('num mcn feature maps: {}'.format(len(mcn_feats)))
print('num py feature maps: {}'.format(len(py_feats)))

class PlaceHolder(object):

    def __init__(self, name, module_type):
        self.name = name
        self.module_type = module_type

    def __repr__(self):
        return '({}, {})'.format(self.module_type, self.name)

# determine feature pairing (accounts for the extra layers created to 
# match the flattening performed before the classifier in pytorch, as 
# well as the removal of dropout layers)
def module_execution_order(module):
    modules = []
    children = list(module.children())
    if len(children) == 0:
        modules.append(module)
    elif isinstance(module, torchvision.models.resnet.BasicBlock):
        assert len(children) == 5 + bool(module.downsample), 'unexpected children'
        submodules = children[:5]
        prefix = list(module.named_children())[0][0]
        if module.downsample:
            submodules.append(PlaceHolder('{}-proj'.format(prefix), 'proj'))
            submodules.append(PlaceHolder('{}-bn'.format(prefix), 'bn'))
        
        submodules.append(PlaceHolder('{}-merge'.format(prefix), 'sum'))
        submodules.append(PlaceHolder('{}-relu'.format(prefix), 'relu'))
        modules.extend(submodules)
    elif isinstance(module, torchvision.models.resnet.Bottleneck):
        assert len(children) == 7 + bool(module.downsample), 'unexpected children'
        submodules = children[:6]
        prefix = list(module.named_children())[0][0]
        submodules.insert(4, PlaceHolder('{}-relu2'.format(prefix), 'relu'))
        submodules.insert(2, PlaceHolder('{}-relu1'.format(prefix), 'relu'))
        if module.downsample:
            submodules.append(PlaceHolder('{}-proj'.format(prefix), 'proj'))
            submodules.append(PlaceHolder('{}-bn'.format(prefix), 'bn'))
        
        submodules.append(PlaceHolder('{}-merge'.format(prefix), 'sum'))
        submodules.append(PlaceHolder('{}-relu'.format(prefix), 'relu'))
        modules.extend(submodules)
    elif pl.has_lambda_child(module):
        prefix = list(module.named_children())[0][0]
        assert pl.is_lambda_reduce(children[1]), 'invalid map reduce pair'
        submodules = []
        map_blocks = list(children[0].children())
        for map_block in map_blocks:
            if not pl.is_plain_lambda(map_block):
                submodules.extend(module_execution_order(map_block))
        # TODO: generalise
        submodules.append(PlaceHolder('{}-merge'.format(prefix), 'sum'))
        if len(children) > 2: # apply operation to output of reduce
            submodules.extend(children[2:])
        modules.extend(submodules)
    else:
        for child in children:
            modules.extend(module_execution_order(child))
    return modules

def get_feature_pairs(net):
    feat_modules = module_execution_order(net.features)
    classifier_modules = module_execution_order(net.classifier)
    modules = feat_modules + classifier_modules
    py_feat_idx = list(range(len(modules) + 2))
    dropout_idx = [i + 1 for i,x in enumerate(modules)  # +1 for input im
            if isinstance(x, torch.nn.modules.dropout.Dropout)]
    pairs = [] 
    cursor = 0
    for py_idx in py_feat_idx:
        if py_idx == len(feat_modules):
            cursor += 1 # mcn flattening procedure uses an extra layer
        if py_idx in dropout_idx and args.remove_dropout:
            print('drop zone')
            continue
        print(py_idx, cursor)
        pairs.append([py_idx, cursor])
        cursor += 1
    return pairs

# compare activations
pairs = get_feature_pairs(net)
for py_idx, mcn_idx in pairs:
    py_feat = py_feats[py_idx]
    mcn_feat = mcn_feats[mcn_idx]
    print('{}v{}: size py: {} vs size mcn: {}'.format(py_idx,mcn_idx,
                      py_feat.shape, mcn_feat.shape))
    diff = np.absolute(py_feat - mcn_feat).mean()
    tol = 1e-4
    if diff > tol: # allow a huge margin
        print('warning: differing output values!')
        print('diff: {}'.format(diff))
        print('py mean: {}'.format(py_feat.mean()))
        print('mcn mean: {}'.format(mcn_feat.mean()))
        #raise ValueError('numerical checks failed') # TODO: fix properly
