import os, sys
import torch
import torch.nn as nn
import torch.nn.functional as F

from sequence_models.trRosetta_utils import *
from sequence_models.constants import WEIGHTS_DIR

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def pad_size(d, k, s):
    return int(((139 * s) - 140 + k + ((k - 1) * (d - 1))) / 2)


class trRosettaBlock(nn.Module):
        
    def __init__(self, dilation):
        
        """Simple convolution block
        
        Parameters:
        -----------
        dilation : int
            dilation for conv
        """

        super(trRosettaBlock, self).__init__()
        self.conv1 = nn.Conv2d(64, 64, kernel_size=3, stride=1, dilation=dilation, padding=pad_size(dilation, 3, 1))
        self.instnorm1 = nn.InstanceNorm2d(64, eps=1e-06, affine=True)
        #         self.dropout1 = nn.Dropout2d(0.15)
        self.conv2 = nn.Conv2d(64, 64, kernel_size=3, stride=1, dilation=dilation, padding=pad_size(dilation, 3, 1))
        self.instnorm2 = nn.InstanceNorm2d(64, eps=1e-06, affine=True)

    def forward(self, x, old_elu, ):
        """
        Parameters:
        -----------
        x : torch.Tensor()
            input tensor
            
        old_elu : torch.Tensor()
            copy of x
        
        Returns:
        --------
        x : torch.Tensor
            output of block

        x.clone() : torch.Tensor
            copy of x
        """
        x = F.elu(self.instnorm1(self.conv1(x)))
        #         x = self.dropout1(x)
        x = F.elu(self.instnorm2(self.conv2(x)) + old_elu)
        return x, x.clone()


class trRosetta(nn.Module):
    
    """trRosetta for single model"""

    def __init__(self, n2d_layers=61, model_id='a', decoder=True):
        """
        Parameters:
        -----------
        model_id : str
            pretrained models a, b, c, d and/or e.
    
        decoder : bool
            whether to run the last layers to produce distance 
            and angle outputs

        """
        super(trRosetta, self).__init__()

        self.conv0 = nn.Conv2d(526, 64, kernel_size=1, stride=1, padding=pad_size(1, 1, 1))
        self.instnorm0 = nn.InstanceNorm2d(64, eps=1e-06, affine=True)

        dilation = 1
        layers = []
        for _ in range(n2d_layers):
            layers.append(trRosettaBlock(dilation))
            dilation *= 2
            if dilation > 16:
                dilation = 1

        self.layers = nn.ModuleList(modules=layers)
        self.decoder = decoder
        if decoder:
            self.softmax = nn.Softmax(dim=1)
            self.conv_theta = nn.Conv2d(64, 25, kernel_size=1, stride=1, padding=pad_size(1, 1, 1))
            self.conv_phi = nn.Conv2d(64, 13, kernel_size=1, stride=1, padding=pad_size(1, 1, 1))
            self.conv_dist = nn.Conv2d(64, 37, kernel_size=1, stride=1, padding=pad_size(1, 1, 1))
            self.conv_bb = nn.Conv2d(64, 3, kernel_size=1, stride=1, padding=pad_size(1, 1, 1))
            self.conv_omega = nn.Conv2d(64, 25, kernel_size=1, stride=1, padding=pad_size(1, 1, 1))
        if model_id is not None:
            self.load_weights(model_id)

    def forward(self, x, ):
        """
        Parameters:
        -----------
        x : torch.Tensor, (1, 526, len(sequence), len(sequence))
            inputs after trRosettaPreprocessing
    
        Returns:
        --------
        dist_probs : torch.Tensor
            distance map probabilities
            
        theta_probs : torch.Tensor
            theta angle map probabilities
            
        phi_probs : torch.Tensor
            phi angle map probabilities
        
        omega_probs: torch..Tensor
            omega angle map probabilities
        
        x : torch.Tensor
            outputs before calculating final layers
        """
        x = F.elu(self.instnorm0(self.conv0(x)))
        old_elu = x.clone()
        for layer in self.layers:
            x, old_elu = layer(x, old_elu)

        if self.decoder:
            logits_theta = self.conv_theta(x)
            theta_probs = self.softmax(logits_theta)

            logits_phi = self.conv_phi(x)
            phi_probs = self.softmax(logits_phi)

            # symmetrize
            x = 0.5 * (x + torch.transpose(x, 2, 3))

            logits_dist = self.conv_dist(x)
            dist_probs = self.softmax(logits_dist)

            logits_omega = self.conv_omega(x)
            omega_probs = self.softmax(logits_omega)

            return dist_probs, theta_probs, phi_probs, omega_probs
        else:
            return x

    def load_weights(self, model_id):
        
        """
        Parameters:
        -----------
        model_id : str
            pretrained models a, b, c, d and/or e.
        """

        path = WEIGHTS_DIR + 'trrosetta_pytorch_weights/' + model_id + '.pt'

        # check to see if pytorch weights exist, if not -> generate
        if not os.path.exists(path):
            tf_to_pytorch_weights(self.named_parameters(), model_id)
        self.load_state_dict(torch.load(path, ), strict=False)


class trRosettaEnsemble(nn.Module):
    """trRosetta ensemble"""
    def __init__(self, model, n2d_layers=61, model_ids='abcde', decoder=True):
        """
        Parameters:
        -----------
        model : class 
            base model to use in ensemble
        
        n2d_layers : int 
            number of layers of the conv block to use for each base model
        
        model_ids: str
            pretrained models to use in the ensemble a, b, c, d and/or e. 
            
        decoder : bool
            if True, return dist, omega, phi, theta; else return layer prior decoder
        
        """

        super(trRosettaEnsemble, self).__init__()
        self.model_list = nn.ModuleList()
        for i in list(model_ids):
            params = {'model_id': i, 'n2d_layers': n2d_layers, 'decoder': decoder}
            self.model_list.append(model(**params).to(device))

    def forward(self, x):
        """
        Parameters:
        -----------
        x : torch.Tensor, (1, 526, len(sequence), len(sequence))
            inputs after trRosettaPreprocessing
        """
        return [mod(x) for mod in self.model_list]

# EXAMPLE
# filename = 'example/T1001.a3m' 
# seqs = parse_a3m(filename) # grab seqs
# tokenizer = Tokenizer(PROTEIN_ALPHABET) 
# seqs = [tokenizer.tokenize(i) for i in seqs] # ohe into our order

# base_model = trRosetta
# input_token_order = PROTEIN_ALPHABET
# ensemble = trRosettaEnsemble(base_model, n2d_layers=61,model_ids='abcde')
# preprocess = trRosettaPreprocessing(input_token_order=PROTEIN_ALPHABET, wmin=0.8)
# x = preprocess.process(seqs)
# with torch.no_grad():
#     ensemble.eval()
#     outputs = ensemble(x.double())
