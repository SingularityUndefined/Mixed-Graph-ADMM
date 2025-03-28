import os
os.environ["KMP_DUPLICATE_LIB_OK"]="TRUE"
import numpy as np
import torch
import os
from utils import *
import matplotlib.pyplot as plt
import math


class ADMM_algorithm():
    '''
    only with 1 head
    '''
    def __init__(self, graph_info, ADMM_info, use_kNN=False, k=4, u_sigma=None, d_sigma=None, expand_time_dim=True, ablation='None', t_in=12, T=24, use_line_graph=False, skip_connection=1):
        self.t_in = t_in
        self.T = T
        self.use_line_graph = use_line_graph
        self.skip_connection = skip_connection
        self.n_nodes = graph_info['n_nodes']
        self.u_edges = graph_info['u_edges']
        self.u_dists = graph_info['u_dist']
        self.use_kNN = use_kNN
        # TODO: further we can use different connection in directed graphs and undirected graphs
        if use_kNN:
            self.connect_list, self.dist_list = k_nearest_neighbors(self.n_nodes, self.u_edges, self.u_dists, k)
            self.connect_list = self.connect_list.to(torch.int64)
        else:
            self.connect_list, self.dist_list = connect_list(self.n_nodes, self.u_edges, self.u_dists)

        self.ablation = ablation
        assert ablation in ['None', 'DGTV', 'DGLR', 'UT'], "ablation should be in [\'None\', \'DGTV\', \'DGLR\', \'UT\']"
        self.u_ew = undirected_graph_from_distance(self.connect_list, self.dist_list, u_sigma=u_sigma, regularized=True)
        if expand_time_dim:
            self.u_ew = expand_time_dimension(self.u_ew, T)
        # if self.ablation != 'UT':
        if not self.use_line_graph:
            self.d_ew = directed_graph_from_distance(self.connect_list, self.dist_list, d_sigma=d_sigma, regularized=True)
            if expand_time_dim:
                self.d_ew = expand_time_dimension(self.d_ew, T-1)
        else:
            # if self.skip_connection > 1:
            self.d_ew = torch.ones((self.n_nodes, self.T, self.skip_connection)) # TODO: change by attention machenism
            self.d_ew.tril_(diagonal=-1)
            self.d_ew[:,0,0].fill_(1)
            self.d_ew = self.d_ew / self.d_ew.sum(-1, keepdim=True)
            self.d_ew[:,0,0].fill_(0)

            self.d_ew = self.d_ew.permute(1, 2, 0) # in (T, skip, N)

            # connection list
            self.time_list = torch.arange(0, self.T).unsqueeze(1) - torch.arange(1, self.skip_connection + 1)
                # self.time_list = torch.zeros((self.T, self.skip_connection)) # in (T, skip)
                # offsets = torch.arange(1, self.T)
                # for offset in offsets:
                #     self.time_list.diagonal(-offset).fill_(offset - 1)
                # reverse edge weights and connections
        
        self.rho = ADMM_info['rho']
        self.rho_u = ADMM_info['rho_u']
        self.rho_d = ADMM_info['rho_d']
        self.mu_u = ADMM_info['mu_u']
        self.mu_d1 = ADMM_info['mu_d1']
        self.mu_d2 = ADMM_info['mu_d2']

        self.alpha_x = []
        self.beta_x = []
        self.alpha_zu = []
        self.beta_zu = []
        self.alpha_zd = []
        self.beta_zd = []
        self.CG_iter_x = []
        self.CG_iter_zu = []
        self.CG_iter_zd = []

        self.max_CG_iter = 100
        self.max_inner_iter = 100
        self.CG_tol = 1e-8
        self.ADMM_tol = 1e-6
        self.max_ADMM_iter = 150

        self.t_in = t_in
        self.T = T

        self.p_res_list = []
        self.d_res_list = []
        self.x_shift_list = []
        self.delta_x_per_step = []
        self.DGTV_list = []
        self.DGLR_list = []
        self.GLR_list = []
        self.recover_list = []
        
        self.res_name = ['zu']
        if self.ablation in ['None', 'DGLR']:
            self.res_name.append('phi')
        if self.ablation != 'DGLR':
            self.res_name.append('zd')
    
    def init_iterations(self, ablation, use_line_graph=False):
        # self.use_line_graph = use_line_graph
        if use_line_graph:
            self.use_line_graph = True
            self.d_ew = torch.ones((self.n_nodes, 1))
        else:
            self.use_line_graph = False
            self.d_ew = directed_graph_from_distance(self.connect_list, self.dist_list, d_sigma=None, regularized=True)

        self.ablation = ablation
        self.alpha_x = []
        self.beta_x = []
        self.alpha_zu = []
        self.beta_zu = []
        self.alpha_zd = []
        self.beta_zd = []
        self.CG_iter_x = []
        self.CG_iter_zu = []
        self.CG_iter_zd = []
        self.p_res_list = []
        self.d_res_list = []
        self.x_shift_list = []
        self.delta_x_per_step = []
        
        self.res_name = ['zu']
        if self.ablation in ['None', 'DGLR']:
            self.res_name.append('phi')
        if self.ablation != 'DGLR':
            self.res_name.append('zd')
        
        self.DGTV_list = []
        self.DGLR_list = []
        self.GLR_list = []


        

    # operators
    def apply_op_Lu(self, x):
        '''
        signal shape: (B, T=24, N, n_channels)
        '''
        B, T, n_channels = x.size(0), x.size(1), x.size(-1)
        pad_x = torch.zeros_like(x[:,:,0:1])
        pad_x = torch.cat((x, pad_x), 2)

        # gather feature on each signals
        weights_features = self.u_ew.unsqueeze(0).unsqueeze(-1) * pad_x[:,:,self.connect_list[:,1:].reshape(-1)].reshape(B, T, self.n_nodes, -1, n_channels)
        return x - weights_features.sum(3)
    
    def apply_op_Ldr(self, x):
        B, T, N, n_channels = x.size(0), x.size(1), x.size(2), x.size(-1)
        if self.use_line_graph:
            if self.skip_connection == 1:
                y = x.clone()
                y[:,0] = x[:,0] * 0
                y[:,1:] = x[:,1:] - x[:,:-1]
                return y
            else:
                # caucluate features
                features = (self.d_ew.view(-1, N)[None,:,:, None] * x[:, self.time_list.view(-1),:,:]).reshape(B, T, -1, N, n_channels)# (N, skip) * (B, T, N, C)
                assert torch.all(features[:,0] == 0), "Features at time 0 should be all zero"
                y = x - features.sum(2)
                y[:,0] = y[:,0] * 0
                return y

        else:
            B, T, n_channels = x.size(0), x.size(1), x.size(-1)
            pad_x = torch.zeros_like(x[:,:,0:1])
            pad_x = torch.cat((x, pad_x), 2)
            # gather features on each children
            child_features = self.d_ew.unsqueeze(0).unsqueeze(-1) * pad_x[:, :-1, self.connect_list.view(-1)].view(B, T-1, self.n_nodes, -1, n_channels)

            y = x.clone()
            y[:,1:] = x[:,1:] - child_features.sum(3)
            # time 0 has no children
            y[:,0] = x[:,0] * 0
            return y
    
    def apply_op_Ldr_T(self, x:torch.tensor):
        B, T, n_channels = x.size(0), x.size(1), x.size(-1)
        if self.use_line_graph:
            if self.skip_connection == 1:
                y = x.clone()
                y[:,0] = x[:,0] * 0
                y[:,:-1] = y[:,:-1] - x[:,1:]
                return y
            else:
                features = self.d_ew[None,:,:,:,None] * x[:,:,None,:,:] # in (B, T, skip, N, C)
                # sum according to diagonals
                features = torch.stack([features.diagonal(offset=-offset, dim1=1, dim2=2).sum(-1) for offset in range(1, T)], dim=1) # in [(B, N, C)] * (T-1) -> (B, T-1, N, C)
                y = x.clone()
                y[:,0] = x[:,0] * 0
                y[:,:-1] = y[:,:-1] - features
                return y
        else:
            B ,T, n_channels = x.size(0), x.size(1), x.size(-1)
            if self.use_kNN:
                # assymetric graph constructions with kNN. Compute weighted features for each father, scatter-add to the childrens

                holder = self.d_ew.unsqueeze(0).unsqueeze(-1) * x[:,1:].unsqueeze(3) # (B, T-1, N, k, n_channels)
                father_features = torch.zeros((B, T-1, self.n_nodes+1, n_channels), dtype=holder.dtype)
                # print(holder.dtype, father_features.dtype)
                index = self.connect_list.reshape(-1)[None, None, :, None].repeat(B, T-1, 1, n_channels)
                index[index == -1] = self.n_nodes
                if torch.any(index < 0) or torch.any(index >= father_features.size(2)):
                    raise ValueError("Index out of bounds")
                
                father_features = father_features.scatter_add(2, index, holder.view(B, T-1, -1, n_channels))
                father_features = father_features[:,:,:-1]
            else:
                # the graph connect list is symmetric. Direct apply on the father nodes
                pad_x = torch.zeros_like(x[:,:,0:1])
                pad_x = torch.cat((x, pad_x), 2)
                father_features = self.d_ew.unsqueeze(0).unsqueeze(-1) * pad_x[:, 1:, self.connect_list.view(-1)].view(B, T-1, self.n_nodes, -1, n_channels)
                father_features = father_features.sum(3)

            # the end nodes (in time T) have no children, y[:, -1] = x
            # the source nodes in time 0 have no fathers, y[:,0] = - father_features[:,0]
            # other nodes: y[:,t] = x[:,t] - father_features[:, t]
            y = x.clone()
            y[:,0] = x[:,0] * 0
            y[:,:-1] = x[:, :-1] - father_features
            return y
    
    def apply_op_cLdr(self, x):
        y = self.apply_op_Ldr(x)
        y = self.apply_op_Ldr_T(y)
        return y
    
    def DGLR(self, x):
        '''
        x in (B, T, N, C)
        return x cLdr x, mean of each batch
        '''
        return (self.apply_op_Ldr(x) ** 2).sum((1,2,3)).mean()
        # return (x * self.apply_op_cLdr(x)).sum((1,2,3)).mean()
    
    def DGTV(self, x):
        '''
        Return: \Vert L^d_r x \Vert_1, mean of each batch
        '''
        # torch.norm()
        return self.apply_op_Ldr(x).norm(dim=[1,2,3], p=1).mean()
    
    def GLR(self, x):
        return (x * self.apply_op_Lu(x)).sum((1,2,3)).mean()
    
    def apply_op_Ln(self, x):
        # change directed graph into undirected ones
        B, T, n_channels = x.size(0), x.size(1), x.size(-1)
        pad_x = torch.zeros_like(x[:,:,0:1])
        pad_x = torch.cat((x, pad_x), 2)
        y = x.clone()
        if self.use_line_graph: # line graph, very simple
            father_features = x[:, :-1]
            child_features = x[:, 1:]
            # regularized
            y[:,1:] = x[:,1:] - father_features / math.sqrt(2)
            y[:,:-1] = x[:,:-1] - child_features / math.sqrt(2)
            return y
        else:
            child_features = self.d_ew.unsqueeze(0).unsqueeze(-1) * pad_x[:, :-1, self.connect_list.view(-1)].view(B, T-1, self.n_nodes, -1, n_channels)
            child_features = child_features.sum(3) # in (B, T-1, N, C)
            child_self_features = self.d_ew.unsqueeze(0).unsqueeze(-1) * x[:, 1:, :, None, :] # in (B, T-1, N, k, C)
            child_self_features = child_self_features.sum(3) - child_features # in (B, T-1, N, C)
            if self.use_kNN:
                holder = self.d_ew.unsqueeze(0).unsqueeze(-1) * x[:,1:].unsqueeze(3) # (B, T-1, N, k, n_channels)
                father_features = torch.zeros((B, T-1, self.n_nodes+1, n_channels), dtype=holder.dtype)
                # print(holder.dtype, father_features.dtype)
                index = self.connect_list.reshape(-1)[None, None, :, None].repeat(B, T-1, 1, n_channels)
                index[index == -1] = self.n_nodes
                if torch.any(index < 0) or torch.any(index >= father_features.size(2)):
                    raise ValueError("Index out of bounds")
                
                father_features = father_features.scatter_add(2, index, holder.view(B, T-1, -1, n_channels))
                father_features = father_features[:,:,:-1]
            else:
                father_features = self.d_ew.unsqueeze(0).unsqueeze(-1) * pad_x[:, 1:, self.connect_list.view(-1)].view(B, T-1, self.n_nodes, -1, n_channels)
                father_features = father_features.sum(3)

                # not regularized, compute self features with weights
            father_self_features = self.d_ew.unsqueeze(0).unsqueeze(-1) * x[:, :-1, :, None, :] # in (B, T-1, N, k, C)
            father_self_features = father_self_features.sum(3) - father_features # in (B, T-1, N, C)

            y = torch.zeros_like(y)
            y[:,1:] = y[:,1:] + child_self_features
            y[:,:-1] = y[:,:-1] + father_self_features
            return y
    # # in one equation, for both line graph and kNN graph
    # def apply_op_Ln(self, x): # undirected graphs
    #     # for undirected graph, each node is to minus its neighboring features
    #     # we don't use further regularization on undirected graphs
    #     B, T, n_channels = x.size(0), x.size(1), x.size(-1)
    #     pad_x = torch.zeros_like(x[:,:,0:1])
    #     pad_x = torch.cat((x, pad_x), 2)
    #     # father and children features
    #     if self.use_line_graph:
    #         father_features = x[:, :-1]
    #         child_features = x[:, 1:]
    #     else:
    #         child_features = self.d_ew.unsqueeze(0).unsqueeze(-1) * pad_x[:, :-1, self.connect_list.view(-1)].view(B, T-1, self.n_nodes, -1, n_channels)
    #         child_features = child_features.sum(3)
    #         if self.use_kNN:
    #             holder = self.d_ew.unsqueeze(0).unsqueeze(-1) * x[:,1:].unsqueeze(3) # (B, T-1, N, k, n_channels)
    #             father_features = torch.zeros((B, T-1, self.n_nodes+1, n_channels), dtype=holder.dtype)
    #             # print(holder.dtype, father_features.dtype)
    #             index = self.connect_list.reshape(-1)[None, None, :, None].repeat(B, T-1, 1, n_channels)
    #             index[index == -1] = self.n_nodes
    #             if torch.any(index < 0) or torch.any(index >= father_features.size(2)):
    #                 raise ValueError("Index out of bounds")
                
    #             father_features = father_features.scatter_add(2, index, holder.view(B, T-1, -1, n_channels))
    #             father_features = father_features[:,:,:-1]
    #         else:
    #             father_features = self.d_ew.unsqueeze(0).unsqueeze(-1) * pad_x[:, 1:, self.connect_list.view(-1)].view(B, T-1, self.n_nodes, -1, n_channels)
    #             father_features = father_features.sum(3)
            
    #     y[:,1:] = y[:,1:] - father_features
    #     y[:,:-1] = y[:,:-1] - child_features
            
    #     # neighboring features (regularized)
    #     weights_features = self.u_ew.unsqueeze(0).unsqueeze(-1) * pad_x[:,:,self.connect_list[:,1:].reshape(-1)].reshape(B, T, self.n_nodes, -1, n_channels)
    #     weights_features = weights_features.sum(3)

    #     return y - weights_features


    
    def CG_solver(self, LHS_func, RHS, x0=None, **kwargs): # TODO: if has no solution, compute least square solution
        '''
        Solving linear systems LHS_func(x) = RHS, B samples at the same time
        Input:
            x0 in (B, T, N, n_channels)
            LHS_func(x, args) in (B, T, N, n_channels)
            RHS in (B, T, N, n_channels)
        '''
        alpha_list = []
        beta_list = []
        if x0 is None:
            x = torch.zeros_like(RHS)
        else:
            x = x0.clone()
        
        r = RHS - LHS_func(x, **kwargs)
        p = r.clone() # in (B, T, N, C)

        r_norm_sq = (r * r).sum((1,2,3)) # in (B,)
        for k in range(self.max_CG_iter):
            Ap = LHS_func(p)
            alpha = r_norm_sq / (p * Ap).sum((1,2,3)) # in (B,)
            alpha_list.append(alpha)
            x = x + alpha[:, None, None, None] * p
            r = r - alpha[:, None, None, None] * Ap

            r_norm_new_sq = (r * r).sum((1,2,3)) # in (B,)
            beta = r_norm_new_sq / r_norm_sq
            beta_list.append(beta)
            r_norm_sq = r_norm_new_sq

            if torch.sqrt(r_norm_sq).max() < self.CG_tol:
                # print(f'{k+1} CG iterations converge')
                return x, k + 1, torch.Tensor(alpha_list), torch.Tensor(beta_list) # iterations
            
            # print(f"CG iteration {k}: total max error {torch.sqrt(r_norm_sq).max():.4g}, alpha in ({alpha.min():.4g}, {alpha.max():.4g}), beta in ({beta.min():.4g}, {beta.max():.4g})")

            p = r + beta[:, None, None, None] * p
        # print(f'CG not converge, total max error {torch.sqrt(r_norm_sq).max():.4g}')
        return x, -1, alpha_list, beta_list


    def LHS_x(self, x, mask=None):
        HtHx = x.clone()
        if mask is None:
            HtHx[:,self.t_in:] = HtHx[:,self.t_in:] * 0
        else:
            HtHx = x * mask

        if self.ablation == 'None':
            output = HtHx + (self.rho_u + self.rho_d) / 2 * x + self.rho / 2 * self.apply_op_cLdr(x)
        elif self.ablation == 'DGTV':
            output = HtHx + (self.rho_u + self.rho_d) / 2 * x
        elif self.ablation == 'DGLR':
            output = HtHx + self.rho / 2 * self.apply_op_cLdr(x) + self.rho_u / 2 * x
        else: # 'UT'
            output = HtHx + (self.rho_u + self.rho_d) / 2 * x
            # output = None # TODO: add undirected version
        return output

    def LHS_zu(self, zu):
        return self.mu_u * self.apply_op_Lu(zu) + self.rho_u / 2 * zu
    
    def LHS_zd(self, zd):
        if self.ablation != 'DGLR':
            return self.mu_d2 * self.apply_op_cLdr(zd) + self.rho_d / 2 * zd
        elif self.ablation == 'UT':
            return self.mu_d2 * self.apply_op_Ln(zd) + self.rho_d / 2 * zd
        else:
            print('Error: LHS_zd')
            return None
    
    def phi_direct(self, x, gamma):
        '''
        phi^{tau+1} = soft_(mu_d1 / rho) (L^d_r x - gamma / rho)
        '''
        s = self.apply_op_Ldr(x) - gamma / self.rho
        d = self.mu_d1 / self.rho
        u = torch.abs(s) - d
        return torch.sign(s) * u * (u > 0)     

    def two_loops(self, y, mask=None, differential=False):
        '''
        two-loops algorithm
        Input:
            y in (B, t_in, N, C)
        Output:
            x in (B, T, N, C)
        '''
        # 1. primal guess of x
        if differential:
            assert mask is None, 'differential mode does not support mask'
            diff_y = get_data_difference(y)
            x = initial_guess(diff_y, self.t_in - 1, self.T - 1)
            x = torch.cat((torch.zeros_like(y[:,0:1]), x), 1)
            x = torch.cumsum(x, dim=1)
            
        if mask is None:
            x = initial_guess(y, self.t_in, self.T)
        else:
            # interpolation
            x = initial_interpolation(y, mask)

        assert not torch.isnan(self.d_ew).any(), 'Directed graph weights d_ew has NaN value'
        assert not torch.isnan(self.u_ew).any(), 'Undirected graph weights u_ew has NaN value'

        if self.ablation in ['None', 'DGLR']:
            gamma = torch.ones_like(x) * 0.1
            phi = self.apply_op_Ldr(x)
            assert not torch.isnan(phi).any(), 'initial phi has NaN value'

        for i in range(self.max_ADMM_iter):
            # minimize x
            gamma_u, gamma_d = torch.ones_like(x) * 0.1, torch.ones_like(x) * 0.1
            zu, zd = x.clone(), x.clone()

            for i in range(self.max_inner_iter):
                x_old = x# .clone()
                zu_old = zu# .clone()
                if self.ablation != 'DGLR':
                    zd_old = zd# .clone()

                Hty = torch.zeros_like(x)
                Hty[:,0:y.size(1)] = y

                if self.ablation == 'DGTV':
                    RHS_x = (self.rho_u * zu + self.rho_d * zd) / 2 - (gamma_u + gamma_d) / 2 + Hty
                elif self.ablation == 'None':
                    RHS_x = self.apply_op_Ldr_T(gamma + self.rho * phi) / 2 + (self.rho_u * zu + self.rho_d * zd) / 2 - (gamma_u + gamma_d) / 2 + Hty
                    assert not torch.isnan(gamma + self.rho * phi).any(), f'NaN exists in ADMM loop {i}: gamma {torch.isnan(gamma).any()}, rho {torch.isnan(self.rho).any()}, phi {torch.isnan(phi).any()}'
                elif self.ablation == 'UT':
                    RHS_x = (self.rho_u * zu + self.rho_d * zd) / 2 - (gamma_u + gamma_d) / 2 + Hty
                elif self.ablation == 'DGLR':
                    RHS_x = self.apply_op_Ldr_T(gamma + self.rho * phi) / 2 + self.rho_u * zu / 2 - gamma_u / 2 + Hty
                # print(torch.isnan(zu).any(), torch.isnan(zd).any())
                    assert not torch.isnan(gamma + self.rho * phi).any(), f'NaN exists in ADMM loop {i}: gamma {torch.isnan(gamma).any()}, rho {torch.isnan(self.rho).any()}, phi {torch.isnan(phi).any()}'

                assert not torch.isnan(RHS_x).any(), f'RHS_x has NaN value in ADMM loop {i}: d_ew {torch.isnan(self.d_ew).any()}; (rho_u, rho_d) has NaN ({torch.isnan(self.rho_u).any()}, {torch.isnan(self.rho_d).any()}); (z_u, z_d) has NaN ({torch.isnan(zu).any()}, {torch.isnan(zd).any()}), (gamma_u, gamma_d) has NaN ({torch.isnan(gamma_u).any()}, {torch.isnan(gamma_d).any()})'
                
                # solve x with zu, zd, update x
                x, CG_iter_x, alpha_x, beta_x = self.CG_solver(self.LHS_x, RHS_x, x_old, mask=mask)
                self.alpha_x.append(alpha_x)
                self.beta_x.append(beta_x)
                self.CG_iter_x.append(CG_iter_x)
                assert not torch.isnan(x).any(), f'RHS_x has NaN value in loop {i}'
                assert not torch.isinf(x).any() and not torch.isinf(x).any(), f'x has inf value in loop {i}'

                # solve zu, zd with x, update zu, zd
                RHS_zu = gamma_u / 2 + self.rho_u / 2 * x
                zu, CG_iter_zu, alpha_zu, beta_zu = self.CG_solver(self.LHS_zu, RHS_zu, zu_old)
                self.alpha_zu.append(alpha_zu)
                self.beta_zu.append(beta_zu)
                self.CG_iter_zu.append(CG_iter_zu)
                assert not torch.isnan(zu).any(), f'zu has NaN value in loop {i}'
                # print('RHS_zu, zu', torch.isnan(RHS_zu).any(), RHS_zu.max(), RHS_zu.min(), torch.isnan(zu).any(), zu.max(), zu.min())
                if self.ablation != 'DGLR':
                    RHS_zd = gamma_d / 2 + self.rho_d / 2 * x
                    zd, CG_iter_zd, alpha_zd, beta_zd = self.CG_solver(self.LHS_zd, RHS_zd, zd_old)
                    self.alpha_zd.append(alpha_zd)
                    self.beta_zd.append(beta_zd)
                    self.CG_iter_zd.append(CG_iter_zd)
                    assert not torch.isnan(zd).any(), f'zd has NaN value in loop {i}'
                    # assert not torch.isinf(RHS_zd).any() and not torch.isinf(-RHS_zd).any(), f'RHS_zd has inf value in loop {i}'
                # update admm params
                gamma_u = gamma_u + self.rho_u * (x - zu)
                if self.ablation != 'DGLR':
                    gamma_d = gamma_d + self.rho_d * (x - zd)

                # TODO: residual

            # update phi
            if self.ablation in ['None', 'DGLR']:
                phi_old = phi.clone()
                # print('executed phi update')
                phi = self.phi_direct(x, gamma)
                assert not torch.isnan(phi).any(), f"phi has NaN value in loop {i}"
                gamma = gamma + self.rho * (phi - self.apply_op_Ldr(x))
                assert not torch.isnan(gamma).any(), f'gamma has NaN {torch.isnan(gamma).any()}'

                # TODO: residual


    def combined_loop(self, y, mask=None, differential=False, print_info=True):
        '''
        Input:
            y in (B, t_in, N, C)
        Output:
            x in (B, T, N, C)
        actually the ADMMBlock accepts x
        '''

        # TODO: primal guess of x
        if differential:
            assert mask is None, 'differential mode does not support mask'
            diff_y = get_data_difference(y)
            x = initial_guess(diff_y, self.t_in - 1, self.T - 1)
            x = torch.cat((torch.zeros_like(y[:,0:1]), x), 1)
            x = torch.cumsum(x, dim=1)
            
        if mask is None:
            x = initial_guess(y, self.t_in, self.T)
        else:
            # interpolation
            x = initial_interpolation(y, mask)

        assert not torch.isnan(self.d_ew).any(), 'Directed graph weights d_ew has NaN value'
        assert not torch.isnan(self.u_ew).any(), 'Undirected graph weights u_ew has NaN value'
        
        gamma_u, gamma_d = torch.ones_like(x) * 0.1, torch.ones_like(x) * 0.1

        if self.ablation in ['None', 'DGLR']:
            gamma = torch.ones_like(x) * 0.1
            phi = self.apply_op_Ldr(x)
            assert not torch.isnan(phi).any(), 'initial phi has NaN value'
            
        zu, zd = x.clone(), x.clone()
        
        for i in range(self.max_ADMM_iter):
            x_old = x# .clone()
            zu_old = zu# .clone()
            if self.ablation != 'DGLR':
                zd_old = zd# .clone()
            # phi_old = phi.clone()
            Hty = torch.zeros_like(x)
            Hty[:,0:y.size(1)] = y
            
                # print(torch.isnan(gamma + self.rho[i] * phi).any(), torch.isnan(gamma).any(), )
            if self.ablation == 'DGTV':
                RHS_x = (self.rho_u * zu + self.rho_d * zd) / 2 - (gamma_u + gamma_d) / 2 + Hty
            elif self.ablation == 'None':
                RHS_x = self.apply_op_Ldr_T(gamma + self.rho * phi) / 2 + (self.rho_u * zu + self.rho_d * zd) / 2 - (gamma_u + gamma_d) / 2 + Hty
                assert not torch.isnan(gamma + self.rho * phi).any(), f'NaN exists in ADMM loop {i}: gamma {torch.isnan(gamma).any()}, rho {torch.isnan(self.rho).any()}, phi {torch.isnan(phi).any()}'
            elif self.ablation == 'UT':
                RHS_x = (self.rho_u * zu + self.rho_d * zd) / 2 - (gamma_u + gamma_d) / 2 + Hty
            elif self.ablation == 'DGLR':
                RHS_x = self.apply_op_Ldr_T(gamma + self.rho * phi) / 2 + self.rho_u * zu / 2 - gamma_u / 2 + Hty
            # print(torch.isnan(zu).any(), torch.isnan(zd).any())
                assert not torch.isnan(gamma + self.rho * phi).any(), f'NaN exists in ADMM loop {i}: gamma {torch.isnan(gamma).any()}, rho {torch.isnan(self.rho).any()}, phi {torch.isnan(phi).any()}'

            assert not torch.isnan(RHS_x).any(), f'RHS_x has NaN value in ADMM loop {i}: d_ew {torch.isnan(self.d_ew).any()}; (rho_u, rho_d) has NaN ({torch.isnan(self.rho_u).any()}, {torch.isnan(self.rho_d).any()}); (z_u, z_d) has NaN ({torch.isnan(zu).any()}, {torch.isnan(zd).any()}), (gamma_u, gamma_d) has NaN ({torch.isnan(gamma_u).any()}, {torch.isnan(gamma_d).any()})'
            
            # solve x with zu, zd, update x
            x, CG_iter_x, alpha_x, beta_x = self.CG_solver(self.LHS_x, RHS_x, x_old, mask=mask)
            self.alpha_x.append(alpha_x)
            self.beta_x.append(beta_x)
            self.CG_iter_x.append(CG_iter_x)
            assert not torch.isnan(x).any(), f'RHS_x has NaN value in loop {i}'
            assert not torch.isinf(x).any() and not torch.isinf(x).any(), f'x has inf value in loop {i}'

            # solve zu, zd with x, update zu, zd
            RHS_zu = gamma_u / 2 + self.rho_u / 2 * x
            zu, CG_iter_zu, alpha_zu, beta_zu = self.CG_solver(self.LHS_zu, RHS_zu, zu_old)
            self.alpha_zu.append(alpha_zu)
            self.beta_zu.append(beta_zu)
            self.CG_iter_zu.append(CG_iter_zu)
            assert not torch.isnan(zu).any(), f'zu has NaN value in loop {i}'
            # print('RHS_zu, zu', torch.isnan(RHS_zu).any(), RHS_zu.max(), RHS_zu.min(), torch.isnan(zu).any(), zu.max(), zu.min())
            if self.ablation != 'DGLR':
                RHS_zd = gamma_d / 2 + self.rho_d / 2 * x
                zd, CG_iter_zd, alpha_zd, beta_zd = self.CG_solver(self.LHS_zd, RHS_zd, zd_old)
                self.alpha_zd.append(alpha_zd)
                self.beta_zd.append(beta_zd)
                self.CG_iter_zd.append(CG_iter_zd)
                assert not torch.isnan(zd).any(), f'zd has NaN value in loop {i}'
                # assert not torch.isinf(RHS_zd).any() and not torch.isinf(-RHS_zd).any(), f'RHS_zd has inf value in loop {i}'

            gamma_u = gamma_u + self.rho_u * (x - zu)
            if self.ablation != 'DGLR':
                gamma_d = gamma_d + self.rho_d * (x - zd)
            # udpata phi
            # phi = self.Phi_PGD(phi, x, gamma, i) # 
            if self.ablation in ['None', 'DGLR']:
                phi_old = phi.clone()
                # print('executed phi update')
                phi = self.phi_direct(x, gamma)
                assert not torch.isnan(phi).any(), f"phi has NaN value in loop {i}"
                gamma = gamma + self.rho * (phi - self.apply_op_Ldr(x))
                assert not torch.isnan(gamma).any(), f'gamma has NaN {torch.isnan(gamma).any()}'
            # criterion

            primal_residual = []
            dual_residual = []

            self.x_shift_list.append(torch.norm(x - x_old).item())
            # dx_per_t = (x - x_old).mean(0).norm(dim=[1,2])
            self.delta_x_per_step.append((x - x_old).mean(0).norm(dim=[1,2])) # in (24)
            # zu
            primal_residual.append(torch.norm(x - zu).item())
            # dual_residual.append(torch.norm(-self.rho_u * (zu - zu_old)).item())
            dual_residual.append(torch.norm(zu - zu_old).item())
            self.GLR_list.append(self.GLR(x))
            if mask is not None:
                Hx = x * mask
            else:
                Hx = x[:, :self.t_in]
            assert Hx.size() == y.size(), f'Hx size {Hx.size()}, y size {y.size()} not equal'
            self.recover_list.append(torch.norm(Hx - y).item())

            if self.ablation in ['None', 'DGLR']:
                primal_residual.append(torch.norm(phi - self.apply_op_Ldr(x)).item())
                # dual_residual.append(torch.norm(-self.rho * self.apply_op_Ldr_T(phi - phi_old)).item())
                dual_residual.append(torch.norm(phi - phi_old).item())
                self.DGTV_list.append(self.DGTV(x))
            
            if self.ablation != 'DGLR':
                primal_residual.append(torch.norm(x - zd).item())
                # dual_residual.append(torch.norm(-self.rho_d * (zd - zd_old)).item())
                dual_residual.append(torch.norm(zd - zd_old).item())
                self.DGLR_list.append(self.DGLR(x))
            
            # p_res, d_res = max(primal_residual), max(dual_residual)
            if print_info:
                print(f'ADMM iters {i}: x_CG_iters {CG_iter_x}, zu_CG_iters {CG_iter_zu}, zd_CG_iters {CG_iter_zd}, pri_err = [{", ".join([f"{err:.4g}" for err in primal_residual])}], dual_err = [{", ".join([f"{err:.4g}" for err in dual_residual])}]')
            self.p_res_list.append(primal_residual)
            self.d_res_list.append(dual_residual)
            # print(f'ADMM iters {i}: pri_err = {p_res}, dual_err = {d_res}')
            if max(primal_residual) < self.ADMM_tol and max(dual_residual) < self.ADMM_tol:
                break
        # self.plot_residual()
        return x
    
    def plot_residual(self, descriptions=None, save_path=None, log_y=False):
        iters = len(self.p_res_list)
        p_res = torch.Tensor(self.p_res_list)
        d_res = torch.Tensor(self.d_res_list)
        x_shift = torch.Tensor(self.x_shift_list).reshape(-1, 1)
        print(p_res.shape, d_res.shape, x_shift.shape)
        res = torch.cat((p_res, d_res, x_shift), 1)
        legend = ['pri_' + s for s in self.res_name] + ['dual_'+ s for s in self.res_name] + ['dual_x']
        residual_dict = {
            # 'pri_x': r'$\Vert x - z^u \Vert_2$',
            'pri_zu': r'$\Vert x - z_u \Vert_2$',
            'pri_phi': r'$\Vert \phi - L^d_r x \Vert_2$',
            'pri_zd': r'$\Vert x - z_d \Vert_2$',
            'dual_zu': r'$\Vert z^u - z_u^{old} \Vert_2$',
            'dual_phi': r'$\Vert \phi - \phi^{old} \Vert_2$',
            'dual_zd': r'$\Vert z_d - z_d^{old} \Vert_2$',
            'dual_x': r'$\Vert x - x^{old} \Vert_2$'
        }
        plt.figure()
        plt.grid()
        plt.plot(torch.arange(0, iters, 1), res)
        plt.legend([residual_dict[l] for l in legend])
        if descriptions is not None:
            plt.title(f'Residuals in ADMM ({descriptions})')
        else:
            plt.title('Residuals in ADMM')
        plt.xlabel('ADMM iterations')
        if log_y:
            plt.yscale('log')
        plt.show()
        if save_path is not None:
            plt.savefig(save_path)
        plt.close()

    def plot_x_per_step(self, save_path=None, show_list=None, start_iters=0, descriptions=None, log_y=False):
        # print(self.delta_x_per_step)
        iters = len(self.delta_x_per_step)
        # dxps = torch.tensor(np.array([item.cpu().detach().numpy() for item in self.delta_x_per_step])) # in (L, 24)
        dxps = torch.stack(self.delta_x_per_step, dim=0)
        # print(dxps.size()) # (np.array([item.cpu().detach().numpy() for item in self.delta_x_per_step])) # in (L, 24)
        if show_list is None:
            show_list = list(range(self.T))
        # legend = [f'dual_x_{i:2d}' for i in show_list]
        plt.figure()
        plt.grid()
        plt.xlabel('ADMM iterations')
        if log_y:
            plt.yscale('log')
        plt.plot(torch.arange(start_iters, iters, 1), dxps[start_iters:,show_list])
        for j in show_list:
            plt.annotate(r'$\Vert\Delta x_{%d}\Vert_2$' % j, (iters-1, dxps[-1, j]), textcoords="offset points", xytext=(0,5), ha='center')
        # plt.legend(legend)
        if descriptions is not None:
            plt.title(f'Delta_x for each time step ({descriptions})')
        else:
            plt.title('Delta_x for each time step')
        plt.show()
        if save_path is not None:
            plt.savefig(save_path)
        plt.close()


    def plot_CG_params(self, descriptions=None, save_path=None, discriptions=None, log_y=False):
        iters = len(self.p_res_list)
        p_res = torch.Tensor(self.p_res_list)
        d_res = torch.Tensor(self.d_res_list)
        res = torch.cat((p_res, d_res), 1)
        legend = ['alpha_' + s for s in self.res_name] + ['beta_'+ s for s in self.res_name]
        plt.figure()
        plt.grid()
        plt.plot(torch.arange(0, iters, 1), res)
        plt.legend(legend)
        if descriptions is not None:
            plt.title(f'CGD params in ADMM ({descriptions})')
        else:
            plt.title('CGD params in ADMM')
        plt.xlabel('ADMM iterations')
        plt.yscale('log')
        plt.show()
        if save_path is not None:
            plt.savefig(save_path)
        plt.close()
    
    def plot_regularization_terms(self, save_path=None, descriptions=None, log_y=False):
        iters = len(self.GLR_list)
        glr = torch.Tensor(self.GLR_list)
        plt.figure()
        plt.grid()
        plt.plot(torch.arange(0, iters, 1), glr, label='GLR')
        if self.ablation != 'DGLR':
            dglr = torch.Tensor(self.DGLR_list)
            plt.plot(torch.arange(0, iters, 1), dglr, label='DGLR')
        if self.ablation in ['DGLR', 'None']:
            dgtv = torch.Tensor(self.DGTV_list)
            plt.plot(torch.arange(0, iters, 1), dgtv, label='DGTV')
        # plot recover error
        recover = torch.Tensor(self.recover_list)
        plt.plot(torch.arange(0, iters, 1), recover, label=r'$\Vert \mathbf{Hx} - \mathbf{y} \Vert_2$')
        plt.legend()
        if descriptions is not None:
            plt.title(f'Regularization terms in ADMM ({descriptions})')
        else:
            plt.title('Regularization terms in ADMM')

        plt.xlabel('ADMM iterations')
        if log_y:
            plt.yscale('log')

        plt.show()
        if save_path is not None:
            plt.savefig(save_path)
        plt.close()




def initial_guess(y, t_in, T):
    '''
    y in (B, t_in, N, C)
    return: x in (B, T, N, C)
    use a simple linear regression to guess x
    '''
    t = torch.arange(0, t_in, 1).to(torch.float)
    # print(t.dtype)
    w = ((t[None,:,None,None] * y).mean(1) - t.mean() * y.mean(1)) / ((t ** 2).mean() - t.mean() ** 2)
    b = y.mean(1) - w * t.mean()
    # print(f'Linear regression: w {w.size()}, b {b.size()}')

    t1 = torch.arange(t_in, T, 1).to(torch.float)
    x_pred = w[:,None,:,:] * t1[None,:,None,None] + b[:,None,:,:]
    x = torch.cat((y, x_pred), 1)
    return x

def initial_interpolation(y, mask):
    '''
    y in (B, T, N, C), y = x * mask
    mask in (B, T, N, C), mask = 1 for observed values
    find an initial interpolation method to recover x
    '''
    # regression on each node / batch, B * N * C in total
    B, T, N, C = y.size()
    t = torch.arange(0, T, 1).to(torch.float).unsqueeze(0).unsqueeze(2).unsqueeze(3).repeat(B, 1, N, C) # in (B, T, N, C)
    n_data = mask.sum(1) # in (B, N, C)
    t_mean = (t * mask).sum(1) / n_data
    y_mean = (y * mask).sum(1) / n_data
    ty_mean = (t * y * mask).sum(1) / n_data
    t2_mean = (t ** 2 * mask).sum(1) / n_data
    w = (ty_mean - t_mean * y_mean) / (t2_mean - t_mean ** 2)
    b = y_mean - w * t_mean
    print(f'Linear regression: w {w.size()}, b {b.size()}')
    assert not torch.isnan(w).any(), 'Initial interpolation w has NaN value'
    assert not torch.isnan(b).any(), 'Initial interpolation b has NaN value'
    x = w * t + b
    assert not torch.isnan(x).any(), 'Initial interpolation x has NaN value'
    # add to the missing values
    # mask == 0 is unknown, mask == 1 is known
    # x = x * mask + y
    x = x * (1 - mask) + y
    # print(y[mask == 0].sum())
    # y[mask == 0] = x[mask == 0]
    assert not torch.isnan(x).any(), 'Initial interpolation x has NaN value'
    return x
    # w = (n_data * (t * y * mask).sum(1) - (t * mask).sum(1) * y.sum(1)) / (n_data * ((t * mask) ** 2).sum(1) - (t * mask).sum(1) ** 2)


if __name__ == '__main__':
    # data_dir
    data_dir = '../datasets/PEMS0X_data'
    # TODO: change here
    dataset = 'PEMS04'
    data_folder = os.path.join(data_dir, dataset)
    data_file = dataset + '.npz'
    graph_csv = dataset + '.csv'
    # data
    traffic_dataset = TrafficDataset(data_folder, data_file, graph_csv)
    print(f"data shape: {traffic_dataset.data.shape}, node number: {traffic_dataset.graph_info['n_nodes']}, edge number: {traffic_dataset.graph_info['n_edges']}")

    # kNNs and graph construction
    k = 4
    nearest_nodes, nearest_dists = k_nearest_neighbors(traffic_dataset.graph_info['n_nodes'], traffic_dataset.graph_info['u_edges'], traffic_dataset.graph_info['u_dist'], k)
    print(f'nearest nodes: {nearest_nodes.shape}, nearest_dists: {nearest_dists.shape}')

    # mixed_graph_from_distance()

    x, y = traffic_dataset.get_data(0)
    print(f'training shape: x: {x.shape}, y: {y.shape}')

# graph construction