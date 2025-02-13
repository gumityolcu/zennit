# This file is part of Zennit
# Copyright (C) 2019-2021 Christopher J. Anders
#
# zennit/canonizers.py
#
# Zennit is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation; either version 3 of the License, or (at your option) any
# later version.
#
# Zennit is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Lesser General Public License for
# more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this library. If not, see <https://www.gnu.org/licenses/>.
'''Functions to produce a canonical form of models fit for LRP'''
from abc import ABCMeta, abstractmethod

import torch

from .core import collect_leaves
from .types import Linear, BatchNorm, ConvolutionTranspose


class Canonizer(metaclass=ABCMeta):
    '''Canonizer Base class.
    Canonizers modify modules temporarily such that certain attribution rules can properly be applied.
    '''

    @abstractmethod
    def apply(self, root_module):
        '''Apply this canonizer recursively on all applicable modules.

        Parameters
        ----------
        root_module: obj:`torch.nn.Module`
            Root module to which to apply the canonizers.

        Returns
        -------
        list
            A list of all applied instances of this class.
        '''
        return []

    @abstractmethod
    def register(self):
        '''Apply the changes of this canonizer.'''

    @abstractmethod
    def remove(self):
        '''Revert the changes introduces by this canonizer.'''

    def copy(self):
        '''Return a copy of this instance.'''
        return self.__class__()


class MergeBatchNorm(Canonizer):
    '''Abstract Canonizer to merge the parameters of batch norms into linear modules.'''
    linear_type = (
        Linear,
    )
    batch_norm_type = (
        BatchNorm,
    )

    def __init__(self):
        super().__init__()
        self.linears = None
        self.batch_norm = None
        self.batch_norm_eps = None
        self.linear_params = None
        self.batch_norm_params = None

    def register(self, linears, batch_norm):
        '''Store the parameters of the linear modules and the batch norm module and apply the merge.

        Parameters
        ----------
        linear: list of obj:`torch.nn.Module`
            List of linear layer with mandatory attributes `weight` and `bias`.
        batch_norm: obj:`torch.nn.Module`
            Batch Normalization module with mandatory attributes
            `running_mean`, `running_var`, `weight`, `bias` and `eps`
        '''
        self.linears = linears
        self.batch_norm = batch_norm

        self.linear_params = [(linear.weight.data, getattr(linear.bias, 'data', None)) for linear in linears]

        self.batch_norm_params = {
            key: getattr(self.batch_norm, key).data for key in ('weight', 'bias', 'running_mean', 'running_var')
        }

        self.batch_norm_eps = batch_norm.eps
        self.merge_batch_norm(self.linears, self.batch_norm)

    def remove(self):
        '''Undo the merge by reverting the parameters of both the linear and the batch norm modules to the state before
        the merge.
        '''
        for linear, (weight, bias) in zip(self.linears, self.linear_params):
            linear.weight.data = weight
            if bias is None:
                linear.bias = None
            else:
                linear.bias.data = bias

        for key, value in self.batch_norm_params.items():
            getattr(self.batch_norm, key).data = value

        self.batch_norm.eps = self.batch_norm_eps

    @staticmethod
    def merge_batch_norm(modules, batch_norm):
        '''Update parameters of a linear layer to additionally include a Batch Normalization operation and update the
        batch normalization layer to instead compute the identity.

        Parameters
        ----------
        modules: list of obj:`torch.nn.Module`
            Linear layers with mandatory attributes `weight` and `bias`.
        batch_norm: obj:`torch.nn.Module`
            Batch Normalization module with mandatory attributes `running_mean`, `running_var`, `weight`, `bias` and
            `eps`
        '''
        denominator = (batch_norm.running_var + batch_norm.eps) ** .5
        scale = (batch_norm.weight / denominator)

        for module in modules:
            original_weight = module.weight.data
            if module.bias is None:
                module.bias = torch.nn.Parameter(
                    torch.zeros(1, device=original_weight.device, dtype=original_weight.dtype)
                )
            original_bias = module.bias.data

            if isinstance(module, ConvolutionTranspose):
                index = (None, slice(None), *((None,) * (original_weight.ndim - 2)))
            else:
                index = (slice(None), *((None,) * (original_weight.ndim - 1)))

            # merge batch_norm into linear layer
            module.weight.data = (original_weight * scale[index])
            module.bias.data = (original_bias - batch_norm.running_mean) * scale + batch_norm.bias

        # change batch_norm parameters to produce identity
        batch_norm.running_mean.data = torch.zeros_like(batch_norm.running_mean.data)
        batch_norm.running_var.data = torch.ones_like(batch_norm.running_var.data)
        batch_norm.bias.data = torch.zeros_like(batch_norm.bias.data)
        batch_norm.weight.data = torch.ones_like(batch_norm.weight.data)
        batch_norm.eps = 0.


class SequentialMergeBatchNorm(MergeBatchNorm):
    '''Canonizer to merge the parameters of all batch norms that appear sequentially right after a linear module.

    Note
    ----
    SequentialMergeBatchNorm traverses the tree of children of the provided module depth-first and in-order.
    This means that child-modules must be assigned to their parent module in the order they are visited in the forward
    pass to correctly identify adjacent modules.
    This also means that activation functions must be assigned in their module-form as a child to their parent-module
    to properly detect when there is an activation function between linear and batch-norm modules.

    '''

    def apply(self, root_module):
        '''Finds a batch norm following right after a linear layer, and creates a copy of this instance to merge
        them by fusing the batch norm parameters into the linear layer and reducing the batch norm to the identity.

        Parameters
        ----------
        root_module: obj:`torch.nn.Module`
            A module of which the leaves will be searched and if a batch norm is found right after a linear layer, will
            be merged.

        Returns
        -------
        instances: list
            A list of instances of this class which modified the appropriate leaves.
        '''
        instances = []
        last_leaf = None
        for leaf in collect_leaves(root_module):
            if isinstance(last_leaf, self.linear_type) and isinstance(leaf, self.batch_norm_type):
                if last_leaf.weight.shape[0] == leaf.weight.shape[0]:
                    instance = self.copy()
                    instance.register((last_leaf,), leaf)
                    instances.append(instance)
            last_leaf = leaf

        return instances


class MergeBatchNormtoRight(MergeBatchNorm):
    '''Canonizer to merge the parameters of all batch norms that appear sequentially right before a linear module.

    Note
    ----
    MergeBatchNormtoRight traverses the tree of children of the provided module depth-first and in-order.
    This means that child-modules must be assigned to their parent module in the order they are visited in the forward
    pass to correctly identify adjacent modules.
    This also means that activation functions must be assigned in their module-form as a child to their parent-module
    to properly detect when there is an activation function between linear and batch-norm modules.

    '''

    @staticmethod
    def convhook(module, x, y):
        x = x[0]
        bias_kernel = module.canonization_params["bias_kernel"]
        pad1, pad2 = module.padding
        # ASSUMING module.kernel_size IS ALWAYS STRICTLY GREATER THAN module.padding
        if pad1 > 0:
            left_margin = bias_kernel[:, :, 0:pad1, :]
            right_margin = bias_kernel[:, :, pad1 + 1:, :]
            middle = bias_kernel[:, :, pad1:pad1 + 1, :].expand(
                1,
                bias_kernel.shape[1],
                x.shape[2] - module.weight.shape[2] + 1,
                bias_kernel.shape[-1]
            )
            bias_kernel = torch.cat((left_margin, middle, right_margin), dim=2)

        if pad2 > 0:
            left_margin = bias_kernel[:, :, :, 0:pad2]
            right_margin = bias_kernel[:, :, :, pad2 + 1:]
            middle = bias_kernel[:, :, :, pad2:pad2 + 1].expand(
                1,
                bias_kernel.shape[1],
                bias_kernel.shape[-2],
                x.shape[3] - module.weight.shape[3] + 1
            )
            bias_kernel = torch.cat((left_margin, middle, right_margin), dim=3)

        if module.stride[0] > 1 or module.stride[1] > 1:
            indices1 = [i for i in range(0, bias_kernel.shape[2]) if i % module.stride[0] == 0]
            indices2 = [i for i in range(0, bias_kernel.shape[3]) if i % module.stride[1] == 0]
            bias_kernel = bias_kernel[:, :, indices1, :]
            bias_kernel = bias_kernel[:, :, :, indices2]
        ynew = y + bias_kernel
        return ynew

    def __init__(self):
        super().__init__()
        self.handles = []

    def apply(self, root_module):
        instances = []
        last_leaf = None
        for leaf in collect_leaves(root_module):
            if isinstance(last_leaf, self.batch_norm_type) and isinstance(leaf, self.linear_type):
                instance = self.copy()
                instance.register((leaf,), last_leaf)
                instances.append(instance)
            last_leaf = leaf

        return instances

    def register(self, linears, batch_norm):
        '''Store the parameters of the linear modules and the batch norm module and apply the merge.

        Parameters
        ----------
        linear: list of obj:`torch.nn.Module`
            List of linear layer with mandatory attributes `weight` and `bias`.
        batch_norm: obj:`torch.nn.Module`
            Batch Normalization module with mandatory attributes
            `running_mean`, `running_var`, `weight`, `bias` and `eps`
        '''
        self.linears = linears
        self.batch_norm = batch_norm
        self.batch_norm_eps = self.batch_norm.eps

        self.linear_params = [(linear.weight.data, getattr(linear.bias, 'data', None)) for linear in linears]

        self.batch_norm_params = {
            key: getattr(self.batch_norm, key).data for key in ('weight', 'bias', 'running_mean', 'running_var')
        }
        returned_handles = self.merge_batch_norm(self.linears, self.batch_norm)
        if returned_handles != []:
            self.handles = self.handles + returned_handles

    def remove(self):
        '''Undo the merge by reverting the parameters of both the linear and the batch norm modules to the state before
        the merge.
        '''
        super().remove()
        for h in self.handles:
            h.remove()
        for module in self.linears:
            if isinstance(module, torch.nn.Conv2d):
                if module.padding != (0, 0):
                    delattr(module, "canonization_params")

    def merge_batch_norm(self, modules, batch_norm):
        return_handles = []
        denominator = (batch_norm.running_var + batch_norm.eps) ** .5

        # Weight of the batch norm layer when seen as an affine transformation
        scale = (batch_norm.weight / denominator)

        # bias of the batch norm layer when seen as an affine transformation
        shift = batch_norm.bias - batch_norm.running_mean * scale

        for module in modules:
            original_weight = module.weight.data
            if module.bias is None:
                module.bias = torch.nn.Parameter(
                    torch.zeros(module.out_channels, device=original_weight.device, dtype=original_weight.dtype)
                )
            original_bias = module.bias.data

            if isinstance(module, ConvolutionTranspose):
                index = (slice(None), *((None,) * (original_weight.ndim - 1)))
            else:
                index = (None, slice(None), *((None,) * (original_weight.ndim - 2)))

            # merge batch_norm into linear layer to the right
            module.weight.data = (original_weight * scale[index])

            if isinstance(module, torch.nn.Conv2d):
                if module.padding == (0, 0):
                    module.bias.data = (original_weight * shift[index]).sum(dim=[1, 2, 3]) + original_bias
                else:
                    # We calculate a bias kernel, which is the output of the conv layer, without the bias, and with maximum padding,
                    # applied to feature maps of the same size as the convolution kernel, with values given by the batch norm biases.
                    # This produces a mostly constant feature map, which is not constant near the edges due to padding.
                    # We then attach a forward hook to the conv layer to compute from this bias_kernel the feature map to be added
                    # after the convolution due to the batch norm bias, depending on the given input's shape
                    bias_kernel = shift[index].expand(*(shift[index].shape[0:-2] + original_weight.shape[-2:]))
                    temp_module = torch.nn.Conv2d(in_channels=module.in_channels, out_channels=module.out_channels,
                                                  kernel_size=module.kernel_size, padding=module.padding,
                                                  padding_mode=module.padding_mode, bias=False)
                    temp_module.weight.data = original_weight
                    bias_kernel = temp_module(bias_kernel).detach()

                    module.canonization_params = {}
                    module.canonization_params["bias_kernel"] = bias_kernel
                    return_handles.append(module.register_forward_hook(MergeBatchNormtoRight.convhook))
            elif isinstance(module, torch.nn.Linear):
                module.bias.data = (original_weight * shift).sum(dim=1) + original_bias

        # change batch_norm parameters to produce identity
        batch_norm.running_mean.data = torch.zeros_like(batch_norm.running_mean.data)
        batch_norm.running_var.data = torch.ones_like(batch_norm.running_var.data)
        batch_norm.bias.data = torch.zeros_like(batch_norm.bias.data)
        batch_norm.weight.data = torch.ones_like(batch_norm.weight.data)
        batch_norm.eps = 0.
        return return_handles


class ThreshReLUMergeBatchNorm(MergeBatchNormtoRight):
    '''Canonizer to canonize BatchNorm -> ReLU -> Linear chains, modifying the ReLU as explained in
    https://github.com/AlexBinder/LRP_Pytorch_Resnets_Densenet/blob/master/canonization_doc.pdf
    '''

    @staticmethod
    def prehook(module, x):
        module.canonization_params["original_x"] = x[0].clone()

    @staticmethod
    def fwdhook(module, x, y):
        x = module.canonization_params["original_x"]
        index = (None, slice(None), *((None,) * (module.canonization_params['weights'].ndim + 1)))
        y = module.canonization_params['weights'][index] * x + module.canonization_params['biases'][index]
        baseline_vals = -1. * (module.canonization_params['biases'] / module.canonization_params['weights'])[index]
        return torch.where(y > 0, x, baseline_vals)

    def __init__(self):
        super().__init__()
        self.relu = None

    def apply(self, root_module):
        instances = []
        oldest_leaf = None
        old_leaf = None
        mid_leaf = None
        for leaf in collect_leaves(root_module):
            if (
                    isinstance(old_leaf, self.batch_norm_type)
                    and isinstance(mid_leaf, ReLU)
                    and isinstance(leaf, self.linear_type)
            ):
                instance = self.copy()
                instance.register((leaf,), old_leaf, mid_leaf)
                instances.append(instance)
            elif (
                    isinstance(oldest_leaf, self.batch_norm_type)
                    and isinstance(old_leaf, ReLU)
                    and isinstance(mid_leaf, AdaptiveAvgPool2d)
                    and isinstance(leaf, self.linear_type)
            ):
                instance = self.copy()
                instance.register((leaf,), oldest_leaf, old_leaf)
                instances.append(instance)
            oldest_leaf = old_leaf
            old_leaf = mid_leaf
            mid_leaf = leaf

        return instances

    def register(self, linears, batch_norm, relu):
        '''Store the parameters of the linear modules and the batch norm module and apply the merge.

        Parameters
        ----------
        linear: list of obj:`torch.nn.Module`
            List of linear layer with mandatory attributes `weight` and `bias`.
        batch_norm: obj:`torch.nn.Module`
            Batch Normalization module with mandatory attributes
            `running_mean`, `running_var`, `weight`, `bias` and `eps`
        relu: obj:`torch.nn.Module`
            The activation unit between the Batch Normalization and Linear modules.
        '''
        self.relu = relu

        denominator = (batch_norm.running_var + batch_norm.eps) ** .5
        scale = (batch_norm.weight / denominator)  # Weight of the batch norm layer when seen as a linear layer
        shift = batch_norm.bias - batch_norm.running_mean * scale  # bias of the batch norm layer when seen as a linear layer
        self.relu.canonization_params = {}
        self.relu.canonization_params['weights'] = scale
        self.relu.canonization_params['biases'] = shift

        super().register(linears, batch_norm)
        self.handles.append(self.relu.register_forward_pre_hook(ThreshReLUMergeBatchNorm.prehook))
        self.handles.append(self.relu.register_forward_hook(ThreshReLUMergeBatchNorm.fwdhook))

    def remove(self):
        '''Undo the merge by reverting the parameters of both the linear and the batch norm modules to the state before
        the merge.
        '''
        super().remove()
        delattr(self.relu, "canonization_params")


class NamedMergeBatchNorm(MergeBatchNorm):
    '''Canonizer to merge the parameters of all batch norms into linear modules, specified by their respective names.

    Parameters
    ----------
    name_map: list[tuple[string], string]
        List of which linear layer names belong to which batch norm name.
    '''

    def __init__(self, name_map):
        super().__init__()
        self.name_map = name_map

    def apply(self, root_module):
        '''Create appropriate merges given by the name map.

        Parameters
        ----------
        root_module: obj:`torch.nn.Module`
            Root module for which underlying modules will be merged.

        Returns
        -------
        instances: list
            A list of merge instances.
        '''
        instances = []
        lookup = dict(root_module.named_modules())

        for linear_names, batch_norm_name in self.name_map:
            instance = self.copy()
            instance.register([lookup[name] for name in linear_names], lookup[batch_norm_name])
            instances.append(instance)

        return instances

    def copy(self):
        return self.__class__(self.name_map)


class AttributeCanonizer(Canonizer):
    '''Canonizer to set an attribute of module instances.
    Note that the use of this Canonizer removes previously set attributes after removal.

    Parameters
    ----------
    attribute_map: Function
        A function that returns either None, if not applicable, or a dict with keys describing which attributes to
        overload for a module. The function signature is (name: string, module: type) -> None or
        dict.
    '''

    def __init__(self, attribute_map):
        self.attribute_map = attribute_map
        self.attribute_keys = None
        self.module = None

    def apply(self, root_module):
        '''Overload the attributes for all applicable modules.

        Parameters
        ----------
        root_module: obj:`torch.nn.Module`
            Root module for which underlying modules will have their attributes overloaded.

        Returns
        -------
        instances : list of obj:`Canonizer`
            The applied canonizer instances, which may be removed by calling `.remove`.
        '''
        instances = []
        for name, module in root_module.named_modules():
            attributes = self.attribute_map(name, module)
            if attributes is not None:
                instance = self.copy()
                instance.register(module, attributes)
                instances.append(instance)
        return instances

    def register(self, module, attributes):
        '''Overload the module's attributes.

        Parameters
        ---------
        module : obj:`torch.nn.Module`
            The module of which the attributes will be overloaded.
        attributes : dict
            The attributes which to overload for the module.
        '''
        self.attribute_keys = list(attributes)
        self.module = module
        for key, value in attributes.items():
            setattr(module, key, value)

    def remove(self):
        '''Remove the overloaded attributes. Note that functions are descriptors, and therefore not direct attributes
        of instance, which is why deleting instance attributes with the same name reverts them to the original
        function.
        '''
        for key in self.attribute_keys:
            delattr(self.module, key)

    def copy(self):
        '''Copy this Canonizer.

        Returns
        -------
        obj:`Canonizer`
            A copy of this Canonizer.
        '''
        return AttributeCanonizer(self.attribute_map)


class CompositeCanonizer(Canonizer):
    '''A Composite of Canonizers, which applies all supplied canonizers.

    Parameters
    ----------
    canonizers : list of obj:`Canonizer`
        Canonizers of which to build a Composite of.
    '''

    def __init__(self, canonizers):
        self.canonizers = canonizers

    def apply(self, root_module):
        '''Apply call canonizers.

        Parameters
        ----------
        root_module: obj:`torch.nn.Module`
            Root module for which underlying modules will have canonizers applied.

        Returns
        -------
        instances : list of obj:`Canonizer`
            The applied canonizer instances, which may be removed by calling `.remove`.
        '''
        instances = []
        for canonizer in self.canonizers:
            instances += canonizer.apply(root_module)
        instances.reverse()
        return instances

    def register(self):
        '''Register this Canonizer. Nothing to do for a CompositeCanonizer.'''

    def remove(self):
        '''Remove this Canonizer. Nothing to do for a CompositeCanonizer.'''
