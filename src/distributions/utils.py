import math
import torch
import torch.nn.functional as F

def rectify(x, method="exp", low=1e-6, high=1e6):
    """ Rectify x to positive 
    Args:
        x (torch.tensor): tensor to be rectified
        method (str, optional): rectification method ["exp", "elu]. Defaults to "exp"
        low (float, optional): lower bound for rectified value. Defaults to 1e-6
        high (float, optional): upper bound for rectified value. Defaults to 1e6
    """
    assert method in ["exp", "elu"]
    if method == "exp":
        low, high = math.log(low), math.log(high)
        out = torch.exp(x.clip(low, high))
    elif method == "elu":
        low, high = math.log(low + 1), high
        out = F.elu(x.clip(low, high)) + 1
    return out

def make_covariance_matrix(logvar, tril=None, cholesky=True, lv_rectify="exp"):
    """ Make full covarance matrix
    
    Args:
        logvar (torch.tensor): log variance vector [batch_size, dim]
        tril (torch.tensor, optional): unmaksed lower triangular matrix [batch_size, dim, dim]. Defaults to None.
        cholesky (bool, optional): return cholesky decomposition. Defaults to False.
        lv_rectify (str, optional): variance rectification method ["exp", "elu], Defaults to "exp".
    Returns:
        L (torch.tensor): scale_tril or cov [batch_size, dim, dim]
    """
    var = rectify(logvar, method=lv_rectify)
    L = torch.diag_embed(var)
    if tril is not None:
        L = L + torch.tril(tril, diagonal=-1)
    
    if not cholesky:
        L = torch.bmm(L, L.transpose(-1, -2))
    return L

def kl_divergence(p, q, eps=1e-6):
    """ Discrete kl divergence """
    assert p.shape[-1] == q.shape[-1]
    log_p = torch.log(p + eps)
    log_q = torch.log(q + eps)
    kl = torch.sum(p * (log_p - log_q), dim=-1)
    return kl