"""Two-layer GraphSAGE encoder + dot-product link decoder (design choice locked in v0)."""

import torch
from torch_geometric.nn import SAGEConv


class GraphSAGE(torch.nn.Module):
    def __init__(self, in_dim: int, hidden_dim: int = 256, out_dim: int = 128):
        super().__init__()
        self.conv1 = SAGEConv(in_dim, hidden_dim)
        self.conv2 = SAGEConv(hidden_dim, out_dim)

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor) -> torch.Tensor:
        h = self.conv1(x, edge_index).relu()
        return self.conv2(h, edge_index)

    @staticmethod
    def decode(z: torch.Tensor, pairs: torch.Tensor) -> torch.Tensor:
        """Logit for each candidate edge, pairs shaped [2, E]."""
        return (z[pairs[0]] * z[pairs[1]]).sum(dim=-1)
