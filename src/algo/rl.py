from copy import deepcopy
import numpy as np
import torch
import torch.nn as nn
from src.distributions.nn_models import Model
from src.distributions.nn_models import MLP
from src.algo.replay_buffer import ReplayBuffer
from src.distributions.utils import kl_divergence

class DoubleQNetwork(Model):
    """ Double Q network for discrete actions and fully observable use """
    def __init__(self, obs_dim, act_dim, hidden_dim, num_hidden, activation="silu"):
        super().__init__()
        self.obs_dim = obs_dim
        self.act_dim = act_dim

        self.q1 = MLP(
            input_dim=obs_dim,
            output_dim=act_dim,
            hidden_dim=hidden_dim,
            num_hidden=num_hidden,
            activation=activation,
            batch_norm=False
        )
        self.q2 = MLP(
            input_dim=obs_dim,
            output_dim=act_dim,
            hidden_dim=hidden_dim,
            num_hidden=num_hidden,
            activation=activation,
            batch_norm=False
        )
    
    def __repr__(self):
        s = "{}(input_dim={}, hidden_dim={}, num_hidden={}, activation={})".format(
            self.__class__.__name__, self.obs_dim, self.q1.hidden_dim, 
            self.q1.num_hidden, self.q1.activation
        )
        return s

    def forward(self, o):
        """ Compute q1 and q2 values
        
        Args:
            o (torch.tensor): observation. size=[batch_size, obs_dim]

        Returns:
            q1 (torch.tensor): q1 value. size=[batch_size, 1]
            q2 (torch.tensor): q2 value. size=[batch_size, 1]
        """
        q1 = self.q1(o)
        q2 = self.q2(o)
        return q1, q2


class SAC(Model):
    """ Soft actor critic """
    def __init__(
        self, agent, hidden_dim, num_hidden, activation, 
        gamma=0.9, beta=0.2, polyak=0.995, norm_obs=False,
        buffer_size=int(1e6), batch_size=100, a_batch_size=32, 
        rnn_len=10, a_steps=50, 
        lr_a=1e-3, lr_c=1e-3, decay=0, grad_clip=None, obs_penalty=1.
        ):
        """
        Args:
            agent (Agent): actor agent
            hidden_dim (int): value network hidden dim
            num_hidden (int): value network hidden layers
            gamma (float, optional): discount factor. Default=0.9
            beta (float, optional): softmax temperature. Default=0.2
            polyak (float, optional): target network polyak averaging factor. Default=0.995
            norm_obs (bool, optional): whether to normalize observations for critic. Default=False
            buffer_size (int, optional): replay buffer size. Default=1e6
            batch_size (int, optional): training batch size. Default=100
            a_steps (int, optional): model update steps per training step. Default=50
            lr_a (float, optional): actor learning rate. Default=1e-3
            lr_c (float, optional): critic learning rate. Default=1e-3
            decay (float, optional): weight decay. Default=0
            grad_clip (float, optional): gradient clipping. Default=None
        """
        super().__init__()
        self.gamma = gamma
        self.beta = beta
        self.polyak = polyak
        self.norm_obs = norm_obs

        self.batch_size = batch_size
        self.a_batch_size = a_batch_size
        self.rnn_len = rnn_len
        self.a_steps = a_steps
        self.lr = lr_a
        self.decay = decay
        self.grad_clip = grad_clip
        self.obs_penalty = obs_penalty

        self.agent = agent

        self.critic = DoubleQNetwork(
            agent.state_dim + agent.obs_dim, agent.act_dim, hidden_dim, num_hidden, activation
        )
        self.critic_target = deepcopy(self.critic)

        # freeze target parameters
        for param in self.critic_target.parameters():
            param.requires_grad = False

        self.critic_optimizer = torch.optim.Adam(
            self.critic.parameters(), lr=lr_c, weight_decay=decay
        )
        self.actor_optimizer = torch.optim.Adam(
            self.agent.parameters(), lr=lr_a, weight_decay=decay
        )
        self.replay_buffer = ReplayBuffer(agent.obs_dim, agent.act_dim, agent.state_dim, buffer_size)
        
        self.obs_mean = nn.Parameter(torch.zeros(agent.obs_dim), requires_grad=False)
        self.obs_variance = nn.Parameter(torch.ones(agent.obs_dim), requires_grad=False)
        
        self.plot_keys = ["eps_return_avg", "critic_loss_avg", "actor_loss_avg"]
    
    def __repr__(self):
        s_agent = self.agent.__repr__()
        s_critic = self.critic.__repr__()
        s = "{}(gamma={}, beta={}, polyak={}, norm_obs={}, "\
            "buffer_size={}, batch_size={}, a_steps={}, "\
            "lr={}, decay={}, grad_clip={}, \n    agent={}, \n    critic={}\n)".format(
            self.__class__.__name__, self.gamma, self.beta, self.polyak, self.norm_obs,
            self.replay_buffer.max_size, self.batch_size, self.a_steps,
            self.lr, self.decay, self.grad_clip, s_agent, s_critic
        )
        return s

    def normalize_obs(self, obs):
        obs_norm = (obs - self.obs_mean) / self.obs_variance**0.5
        return obs_norm
    
    def update_normalization_stats(self):
        if self.norm_obs:
            mean = torch.from_numpy(self.replay_buffer.moving_mean).to(torch.float32)
            variance = torch.from_numpy(self.replay_buffer.moving_variance).to(torch.float32)

            self.obs_mean.data = mean
            self.obs_variance.data = variance
            
            if hasattr(self.agent, "obs_mean"):
                self.agent.obs_mean.data = mean
                self.agent.obs_variance.data = variance
    
    def reset(self):
        self.agent.reset()
        
    def choose_action(self, obs):
        obs = torch.from_numpy(obs).view(1, -1).to(torch.float32)#.to(self.device)
        with torch.no_grad():
            ctl = self.agent.choose_action(obs)
        return ctl.squeeze(0).numpy()

    def compute_critic_loss(self):
        batch = self.replay_buffer.sample_random(self.batch_size)
        state = batch["state"]
        obs = batch["obs"]
        ctl = batch["ctl"]
        r = batch["rwd"]
        next_state = batch["next_state"]
        next_obs = batch["next_obs"]
        done = batch["done"]
        
        # normalize observation
        obs_norm = self.normalize_obs(obs)
        next_obs_norm = self.normalize_obs(next_obs)
        
        with torch.no_grad():    
            # compute value target
            q1_next, q2_next = self.critic_target(torch.cat([next_state, next_obs_norm], dim=-1))
            q_next = torch.min(q1_next, q2_next)
            v_next = torch.logsumexp(q_next / self.beta, dim=-1, keepdim=True) * self.beta
            q_target = r + (1 - done) * self.gamma * v_next
        
        q1, q2 = self.critic(torch.cat([state, obs_norm], dim=-1))
        q1 = torch.gather(q1, -1, ctl.long())
        q2 = torch.gather(q2, -1, ctl.long())
        q1_loss = torch.pow(q1 - q_target, 2).mean()
        q2_loss = torch.pow(q2 - q_target, 2).mean()
        q_loss = (q1_loss + q2_loss) / 2
        return q_loss

    def compute_actor_loss(self):
        batch = self.replay_buffer.sample_episodes(self.a_batch_size, self.rnn_len, prioritize=False)
        pad_batch, mask = batch
        state = pad_batch["state"].to(self.device)
        obs = pad_batch["obs"].to(self.device)
        ctl = pad_batch["ctl"].to(self.device)
        mask = mask.to(self.device)

        # normalize observation
        obs_norm = self.normalize_obs(obs)

        [_, alpha_a], _ = self.agent(obs, ctl)
        
        q1, q2 = self.critic(torch.cat([state, obs_norm], dim=-1))
        q = torch.min(q1, q2)
        a_target = torch.softmax(q / self.beta, dim=-1)
        a_loss = kl_divergence(alpha_a, a_target)#.mean()
        a_loss = torch.sum(a_loss * mask) / (mask.sum() + 1e-6)
        return a_loss
    
    def compute_obs_loss(self):
        batch = self.replay_buffer.sample_episodes(self.a_batch_size, self.rnn_len, prioritize=False)
        pad_batch, mask = batch
        obs = pad_batch["obs"].to(self.device)
        ctl = pad_batch["ctl"].to(self.device)
        
        out = self.agent(obs, ctl)
        obs_loss, _ = self.agent.obs_loss(obs, ctl, mask, out)
        obs_loss = obs_loss.mean()
        return obs_loss

    def take_gradient_step(self, logger=None):
        self.critic.train()
        self.agent.train()
        self.update_normalization_stats()
        
        critic_loss_epoch = []
        actor_loss_epoch = []
        obs_loss_epoch = []
        for i in range(self.a_steps):
            # train critic
            critic_loss = self.compute_critic_loss()
            critic_loss.backward()
            if self.grad_clip is not None:
                nn.utils.clip_grad_norm_(self.critic.parameters(), self.grad_clip)
            self.critic_optimizer.step()
            self.critic_optimizer.zero_grad()
            self.actor_optimizer.zero_grad()

            critic_loss_epoch.append(critic_loss.data.item())

            # train actor
            actor_loss = self.compute_actor_loss()
            obs_loss = self.compute_obs_loss()
            actor_total_loss = actor_loss + self.obs_penalty * obs_loss
            actor_total_loss.backward()
            if self.grad_clip is not None:
                nn.utils.clip_grad_norm_(self.agent.parameters(), self.grad_clip)
            self.actor_optimizer.step()
            self.actor_optimizer.zero_grad()
            self.critic_optimizer.zero_grad()

            actor_loss_epoch.append(actor_loss.data.item())
            obs_loss_epoch.append(obs_loss.data.item())
            
            # update target networks
            with torch.no_grad():
                for p, p_target in zip(
                    self.critic.parameters(), self.critic_target.parameters()
                ):
                    p_target.data.mul_(self.polyak)
                    p_target.data.add_((1 - self.polyak) * p.data)
            
            if logger is not None:
                logger.push({
                    "critic_loss": critic_loss.data.item(),
                    "actor_loss": actor_loss.data.item(),
                    "obs_loss": obs_loss.cpu().data.item(),
                })

        stats = {
            "critic_loss": np.mean(critic_loss_epoch),
            "actor_loss": np.mean(actor_loss_epoch),
            "obs_loss": np.mean(obs_loss_epoch),
        }
        self.critic.eval()
        self.agent.eval()
        return stats

    def on_epoch_end(self):
        pass