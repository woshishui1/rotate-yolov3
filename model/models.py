import torch.nn.functional as F

from utils.google_utils import *
from utils.parse_config import *
from utils.utils import *
from model.model_utils import *

class Swish(nn.Module):
    def __init__(self):
        super(Swish, self).__init__()

    def forward(self, x):
        return x * torch.sigmoid(x)


class SELayer(nn.Module):
    def __init__(self, channel, reduction=16):
        super(SELayer, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Sequential(
            nn.Linear(channel, channel // reduction, bias=False),
            nn.ReLU(inplace=True),
            nn.Linear(channel // reduction, channel, bias=False),
            nn.Sigmoid()
        )

    def forward(self, x):
        b, c, _, _ = x.size()
        y = self.avg_pool(x).view(b, c)
        y = self.fc(y).view(b, c, 1, 1)
        return x * y.expand_as(x)




def create_modules(module_defs, arc, hyp):
    # Constructs module list of layer blocks from module configuration in module_defs

    hyperparams = module_defs.pop(0)    # hyperparams是设置的超参数，pop出来就只剩下模型单元了
    # 这个参数比较还重要！经过一个层（这里记录的是conv,route,shortcut）后如果输出的通道数会改变，卷积核个数也就是计算的输出通道数会被添加这个list，便于后面配置卷积层参数的输入通道（初始这里设置图像通道数3）
    output_filters = [int(hyperparams['channels'])]     
    module_list = nn.ModuleList()
    routes = []  # list of layers which route to deeper layes,融合的层，残差连接和特征融合层的index都存在这里
    yolo_index = -1

    for i, mdef in enumerate(module_defs):
        modules = nn.Sequential()   # 读一个block就放一个Sequential，如果是卷积层就直接conv+bn+relu安排上；如果是route层，则Sequential空着提供站位符的作用
        #卷积层（75个）
        if mdef['type'] == 'convolutional':
            bn = int(mdef['batch_normalize'])   #bn是bool开关，cfg中batch_normalize是1需要加bn层，为0不加（cfg是全加的）
            filters = int(mdef['filters'])      # 卷及核个数和尺寸
            kernel_size = int(mdef['size'])
            pad = (kernel_size - 1) // 2 if int(mdef['pad']) else 0

            modules.add_module('Conv2d', nn.Conv2d(in_channels=output_filters[-1],
                                                out_channels=filters,
                                                kernel_size=kernel_size,
                                                stride=int(mdef['stride']),
                                                padding=pad,
                                                bias=not bn))    # 注意有BN就不加bias，二者等效
            if bn:
                modules.add_module('BatchNorm2d', nn.BatchNorm2d(filters, momentum=0.1))
            if mdef['activation'] == 'leaky':   # TODO: activation study https://github.com/ultralytics/yolov3/issues/441
                # modules.add_module('activation', nn.LeakyReLU(0.1, inplace=True))
                modules.add_module('activation', nn.PReLU(num_parameters=1, init=0.10))  
                # modules.add_module('activation', Swish())

        elif mdef['type'] == 'd-convolutional':
            bn = int(mdef['batch_normalize'])
            filters = int(mdef['filters'])
            modules.add_module('Conv2d', nn.Sequential())
            if bn:
                modules.add_module('BatchNorm2d', nn.BatchNorm2d(filters, momentum=0.1))
            if mdef['activation'] == 'leaky':   
                modules.add_module('activation', nn.LeakyReLU(0.1, inplace=True))
                # modules.add_module('activation', nn.PReLU(num_parameters=1, init=0.10))   
                # modules.add_module('activation', Swish())

        elif mdef['type'] == 'maxpool':
            kernel_size = int(mdef['size'])
            stride = int(mdef['stride'])
            maxpool = nn.MaxPool2d(kernel_size=kernel_size, stride=stride, padding=int((kernel_size - 1) // 2))
            if kernel_size == 2 and stride == 1:  # yolov3-tiny
                modules.add_module('ZeroPad2d', nn.ZeroPad2d((0, 1, 0, 1)))
                modules.add_module('MaxPool2d', maxpool)
            else:
                modules = maxpool

        elif mdef['type'] == 'se':
            channels = int(mdef['channels'])
            modules = SELayer(channels)

        elif mdef['type'] == 'upsample':
            modules = nn.Upsample(scale_factor=int(mdef['stride']), mode='nearest')

        # 特征融合concat层的标识
        # 注意：route和shortcut的层在这里都不处理Sequential()是空的，只是把两者要融合的index放到routes list中去，后面forward再构建连接。
        # route:[1, 5, 8, 12, 15, 18, 21, 24, 27, 30, 33, 37, 40, 43, 46, 49, 52, 55, 58, 62, 65, 68, 71, 79, 85, 61, 91, 97, 36]
        # 共四处route，layer分别是-4,(-1,61),-4,(-1,36)，传入route的参数是79, 85, 61, 91, 97, 36
        # ！添加到route列表的数字指向该层哪、要融合的那层的index!
        elif mdef['type'] == 'route':  
            layers = [int(x) for x in mdef['layers'].split(',')]
            filters = sum([output_filters[i + 1 if i > 0 else i] for i in layers])      #卷积核数目为两层的加和:负数就是取之前的层卷积核数为通道；正数要+1取出后面的是输出
            routes.extend([l if l > 0 else l + i for l in layers])
            # if mdef[i+1]['type'] == 'reorg3d':
            #     modules = nn.Upsample(scale_factor=1/float(mdef[i+1]['stride']), mode='nearest')  # reorg3d

        # shortcut即ResNet的残差连接层
        # 共23个残差block，是sum加和的层，所以参数全都是-3，结合当前层和倒数第三个的（-1,-2层分别是侧分支3*3和1*1卷积）
        # ！添加到route列表的数字指向该层哪、要融合的那层的index!
        elif mdef['type'] == 'shortcut':  # nn.Sequential() placeholder for 'shortcut' layer
            filters = output_filters[int(mdef['from'])]     # sum输出通道和输入的相同
            layer = int(mdef['from'])   # shortcut的layer全是-3
            routes.extend([i + layer if layer < 0 else layer])   # i是当前模块的index，-3后就是往前数三个

        elif mdef['type'] == 'reorg3d':  # yolov3-spp-pan-scale
            # torch.Size([16, 128, 104, 104])
            # torch.Size([16, 64, 208, 208]) <-- # stride 2 interpolate dimensions 2 and 3 to cat with prior layer
            pass

        elif mdef['type'] == 'yolo':
            yolo_index += 1   # 从0开始
            mask_range = [int(i) for i in mdef['mask'].split('-')]
            mask = [i for i in range(mask_range[0],mask_range[1]+1)]
            modules = YOLOLayer(anchors=mdef['anchors'][mask],  # anchor list，用mask提取特定的三个anchor尺度，eg.array([[116,90],[156,198],[373,326]])
                                nc=int(mdef['classes']),  # number of classes,eg.20
                                hyp = hyp,
                                yolo_index=yolo_index,  # 0, 1 or 2三层
                                arc=arc)  # yolo architecture

            # Initialize preceding Conv2d() bias (https://arxiv.org/pdf/1708.02002.pdf section 3.3)
            try:
                if arc == 'defaultpw' or arc == 'Fdefaultpw':  # default with positive weights
                    b = [-4, -3.6]  # obj, cls
                elif arc == 'default':  # default no pw (40 cls, 80 obj)    # 默认是这个
                    b = [-5.5, -4.0]
                elif arc == 'uBCE':  # unified BCE (80 classes)
                    b = [0, -8.5]
                elif arc == 'uCE':  # unified CE (1 background + 80 classes)
                    b = [10, -0.1]
                elif arc == 'Fdefault':  # Focal default no pw (28 cls, 21 obj, no pw)
                    b = [-2.1, -1.8]
                elif arc == 'uFBCE' or arc == 'uFBCEpw':  # unified FocalBCE (5120 obj, 80 classes)
                    b = [0, -6.5]
                elif arc == 'uFCE':  # unified FocalCE (64 cls, 1 background + 80 classes)
                    b = [7.7, -1.1]
                # 提取yolo层之前那个卷积的bias，torch.Size([3, 8])，3是anchor数目，8=5(xywhc,c为出现物体的置信度confidence)+3(类别数)
                bias = module_list[-1][0].bias.view(len(mask), -1)  # 255 to 3x85  
                bias[:, 5]  += b[0] - bias[:, 5 ].mean()  # obj
                bias[:, 6:] += b[1] - bias[:, 6:].mean()  # cls
                # bias = torch.load('weights/yolov3-spp.bias.pt')[yolo_index]  # list of tensors [3x85, 3x85, 3x85]
                module_list[-1][0].bias = torch.nn.Parameter(bias.view(-1))
                # utils.print_model_biases(model)
            except:
                print('WARNING: smart bias initialization failure.')

        else:
            print('Warning: Unrecognized Layer Type: ' + mdef['type'])

        # Register module list and number of output filters
        module_list.append(modules)     #  存放未连接的模型
        output_filters.append(filters)  #  存放输出通道数变化的维度（用处是计算route层的融合的通道数，不会被返回的）

    return module_list, routes   # 最终返回的是modulelist和融合的通道位置





class YOLOLayer(nn.Module):
    def __init__(self, anchors, nc, yolo_index, arc, hyp): 
        super(YOLOLayer, self).__init__()
        self.anchors = torch.Tensor(anchors)
        self.na = len(anchors)  # number of anchors (3)
        self.nc = nc  # number of classes (80)
        self.nx = 0  # initialize number of x gridpoints    
        self.ny = 0  # initialize number of y gridpoints
        self.arc = arc
        self.hyp = hyp
        self.yolo_index = yolo_index  # idx: 0 1 2 ...


    def forward(self, p, img_size, var=None):   # p是特征图，img_size是缩放并padding后的尺寸如torch.Size([320, 416])（用来确定原图和特征图的对应位置）
        bs, ny, nx = p.shape[0], p.shape[-2], p.shape[-1]   # ny nx是特征图的高和宽
        if (self.nx, self.ny) != (nx, ny):
            create_grids(self, img_size, (nx, ny), p.device, p.dtype)   # 缩放anchor到特征图尺寸;用特征图像素编码grid cell

        # p.view(bs, 255, 13, 13) -- > (bs, 3, 13, 13, 85)  # (bs, anchors, grid, grid, classes + xywh)
        p = p.view(bs, self.na, self.nc + 6, self.ny, self.nx).permute(0, 1, 3, 4, 2).contiguous()  # prediction

        # self继承自nn.Module，其自带属性self.training且默认为True，但是在model.eval()会被设置成False
        if self.training:
            # 如果是training,直接返回yolo fp (bs, anchors, grid, grid, classes + xywh)
            return p
        
        else:   # inference   # 不止返回inference结果还有train的
            # s = 1.5  # scale_xy  (pxy = pxy * s - (s - 1) / 2)
            io = p.clone()  # inference output
            io[..., 0:2] = torch.sigmoid(io[..., 0:2]) + self.grid_xy  # xy ：预测的偏移 + grid cell id
            io[..., 2:4] = torch.exp(io[..., 2:4]) * self.anchor_wh[...,:-1]    # wh yolo method （加exp化为正数）；wh预测的是一个比例，基准是anchor
            io[..., 4]   = torch.atan(io[..., 4]) + self.anchor_wh[...,-1]
            # 从特征图放大到原图尺寸
            io[..., :4] *= self.stride
            # 整体缩放法
            # io[..., 2:4] /= self.hyp['context_factor']    
            # 取h短边合理缩放
            io[..., 3] /= self.hyp['context_factor']
            io[..., 2] -= io[..., 3]*(self.hyp['context_factor']-1)

            if 'default' in self.arc:  # seperate obj and cls
                # 将obj得分和各类别的得分进行sigmoid处理
                torch.sigmoid_(io[..., 5:])     # in-place操作，慎用
            elif 'BCE' in self.arc:  # unified BCE (80 classes)
                torch.sigmoid_(io[..., 6:])
                io[..., 5] = 1
            elif 'CE' in self.arc:  # unified CE (1 background + 80 classes)
                io[..., 5:] = F.softmax(io[..., 4:], dim=4)
                io[..., 5] = 1

            if self.nc == 1:
                io[..., 6] = 1  # single-class model https://github.com/ultralytics/yolov3/issues/235

            # 注意：yolo层返回两个张量
            #   - 一个是三个维度的(分类和置信度得分归一化了)         [1, 507, 85]
            #   - 一个是输入reshape分分离出不同类别得分而已    [1, 3, 13, 13, 85]
            # reshape from [1, 3, 13, 13, 85] to [1, 507, 85]
            return io.view(bs, -1, 6 + self.nc), p


class Darknet(nn.Module):
    # YOLOv3 object detection model
    def __init__(self, cfg, hyp, arc='default'):
        super(Darknet, self).__init__()    
        self.module_defs = parse_model_cfg(cfg)     # 返回包含cfg组件dict的list便于调用
        self.module_list, self.routes = create_modules(self.module_defs, arc, hyp)  # 搭建模型（只是堆叠没有连接，连接实现在forword，动态图）以及要融合的位置index（残差结构和多尺度concat两部分）
        self.yolo_layers = get_yolo_layers(self)    # yolo层的index: [82, 94, 106]
        self.hyp = hyp

        # Darknet Header https://github.com/AlexeyAB/darknet/issues/2914#issuecomment-496675346
        # 关于darknet版本的问题
        self.version = np.array([0, 2, 5], dtype=np.int32)  # (int32) version info: major, minor, revision
        self.seen = np.array([0], dtype=np.int64)  # (int64) number of images seen during training

    def forward(self, x, var=None):     # x是传入的缩放和padding后的像素矩阵
        img_size = x.shape[-2:] # 取出hw
        layer_outputs = []      # 所有route层的输出
        output = []             # 三个yolo层的输出

        # zip的是配置文件和占位的模型层（注：cfg文件有108个block，除去第一个net超参数外，剩下的107个在self.module_defs中，和 self.module_list一一对应可以zip）
        for i, (mdef, module) in enumerate(zip(self.module_defs, self.module_list)):
            # print(i)
            mtype = mdef['type']
            # 大多数层定义好了forward，直接调用就行，如下面的第一个接口
            if mtype in ['convolutional', 'upsample', 'maxpool','d-convolutional','se']:
                if 'weight_from' in mdef.keys():
                    shared_weight = int(mdef['weight_from']) 
                    bn = int(mdef['batch_normalize'])   
                    filters = int(mdef['filters'])    
                    kernel_size = int(mdef['size'])
                    pad = (kernel_size - 1) // 2 if int(mdef['pad']) else 0
                    dilation = 1
                    if mtype=='d-convolutional':
                        dilation = (int(mdef['dilation'][1]),int(mdef['dilation'][3])) 
                        pad = dilation if int(mdef['pad']) else 0
                    x = nn.functional.conv2d(x,self.module_list[shared_weight].Conv2d.weight,padding=pad, dilation=dilation)
                else:
                    x = module(x)

            elif mtype == 'route':         
                layers = [int(x) for x in mdef['layers'].split(',')]    #[-4],[-1,61],[-4],[-1,36]
                if len(layers) == 1:
                    x = layer_outputs[layers[0]]    # 这种层是取出yolo往前的四层得到参数进入下一个yolo分支
                else:
                    try:
                        x = torch.cat([layer_outputs[i] for i in layers], 1)    #按第一维度（刨去batch维）concat融合
                    except:  # apply stride 2 for darknet reorg layer
                        layer_outputs[layers[1]] = F.interpolate(layer_outputs[layers[1]], scale_factor=[0.5, 0.5])
                        x = torch.cat([layer_outputs[i] for i in layers], 1)
                    # print(''), [print(layer_outputs[i].shape) for i in layers], print(x.shape)

            elif mtype == 'shortcut':
                x = x + layer_outputs[int(mdef['from'])]    # 残差连接，加和其上一层-1（当前层不是-1执行最后才append）和往上数第三层(-3)
            
            elif mtype == 'yolo':
                # # 注意：yolo层return的有两个张量,x是一个包含两种张量的tuple
                x = module(x, img_size) 
                output.append(x)                                                  
            layer_outputs.append(x if i in self.routes else [])     # 添加所有route层的输出

        if self.training:
            # 注意训练阶段时,返回的是三张yolo层的特征图
            return output
       
        else:
            # 每个yolo层输出一个2张量的tuple，三个yolo最后的output为[(a1,a2)),(b1,b2),(c1,c2)]的形式，unzip后为[(a1,b1,c1),(a2,b2,c2)]
            # [(a1,b1,c1),(a2,b2,c2)]分别是io和p;前者是3维度，后者5维度
            io, p = list(zip(*output))  # inference output, training output
            return torch.cat(io, 1), p  # 保留bs(1)和数据(5+classes)维度,将中间的proposal进行concat(每个yolo层预测其特征图的w*h*3个proposal)

    def fuse(self):
        # Fuse Conv2d + BatchNorm2d layers throughout model
        fused_list = nn.ModuleList()
        for a in list(self.children())[0]:
            if isinstance(a, nn.Sequential):
                for i, b in enumerate(a):
                    if isinstance(b, nn.modules.batchnorm.BatchNorm2d):
                        # fuse this bn layer with the previous conv2d layer
                        conv = a[i - 1]
                        fused = torch_utils.fuse_conv_and_bn(conv, b)
                        a = nn.Sequential(fused, *list(a.children())[i + 1:])
                        break
            fused_list.append(a)
        self.module_list = fused_list
        # model_info(self)  # yolov3-spp reduced from 225 to 152 layers

