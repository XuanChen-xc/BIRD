import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
# import torchvision.transforms as T
from torch.autograd import grad

from paad_rl.a2c_ppo_acktr.algo.kfac import KFACOptimizer
from paad_rl.a2c_ppo_acktr.utils import init, attention_map

import matplotlib.pyplot as plt
class A2C_ACKTR():
    def __init__(self,
                 actor_critic,
                 value_loss_coef,
                 entropy_coef,
                 lr=None,
                 eps=None,
                 alpha=None,
                 max_grad_norm=None,
                 acktr=False,
                 beta=False,
                 imitate=False):

        self.actor_critic = actor_critic
        self.acktr = acktr
        self.beta = beta

        self.value_loss_coef = value_loss_coef
        self.entropy_coef = entropy_coef

        self.max_grad_norm = max_grad_norm

        init_ = lambda m: init(
            m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0),
            gain=0.01)
        self.feature_reg = init_(nn.Linear(32 * 7 * 7, 32 * 7 * 7))
        self.feature_reg.to(self.actor_critic.device)

        if imitate:
            self.optimizer = optim.RMSprop(
                actor_critic.parameters(), lr, eps=eps, alpha=alpha)
        else:
            if acktr:
                self.optimizer = KFACOptimizer(actor_critic)
            else:
                self.optimizer = optim.RMSprop(list(actor_critic.parameters()) + list(self.feature_reg.parameters()) , lr, eps=eps, alpha=alpha)
                
    def unset_imitate(self, lr, eps, alpha):
        if self.acktr:
            self.optimizer = KFACOptimizer(self.actor_critic)
        else:
            self.optimizer = optim.RMSprop(self.actor_critic.parameters(), lr, eps=eps, alpha=alpha)
    
    def update(self, rollouts, org_agent=None, args=None):
        obs_shape = rollouts.obs.size()[2:]
        action_shape = rollouts.actions.size()[-1]
        num_steps, num_processes, _ = rollouts.rewards.size()

        values, action_log_probs, dist_entropy, _ = self.actor_critic.evaluate_actions(
            rollouts.obs[:-1].view(-1, *obs_shape),
            rollouts.recurrent_hidden_states[0].view(
                -1, self.actor_critic.recurrent_hidden_state_size),
            rollouts.masks[:-1].view(-1, 1),
            rollouts.actions.view(-1, action_shape),
            beta=self.beta)
            
        values = values.view(num_steps, num_processes, 1)
        action_log_probs = action_log_probs.view(num_steps, num_processes, 1)

        advantages = rollouts.returns[:-1] - values
        value_loss = advantages.pow(2).mean()

        action_loss = -(advantages.detach() * action_log_probs).mean()

        if args.kl:
            with torch.no_grad():
                correct_dist = org_agent.get_dist(
                    rollouts.clean_obs[:-1].view(-1, *obs_shape),
                rollouts.recurrent_hidden_states[0].view(
                    -1, self.actor_critic.recurrent_hidden_state_size),
                rollouts.masks[:-1].view(-1, 1))

            kl_loss = torch.distributions.kl_divergence(correct_dist, self.actor_critic.fix_dist).mean()
            kl_loss = torch.where(torch.isfinite(kl_loss), kl_loss, torch.tensor(10e5).to(values.device)) 
        else:
            kl_loss = 0.0
        
        if self.acktr and self.optimizer.steps % self.optimizer.Ts == 0:
            # Compute fisher, see Martens 2014
            self.actor_critic.zero_grad()
            pg_fisher_loss = -action_log_probs.mean()
            
            value_noise = torch.randn(values.size())
            if values.is_cuda:
                value_noise = value_noise.to(values.device)

            sample_values = values + value_noise
            vf_fisher_loss = -(values - sample_values.detach()).pow(2).mean()

            fisher_loss = pg_fisher_loss + vf_fisher_loss
            self.optimizer.acc_stats = True
            fisher_loss.backward(retain_graph=True)
            self.optimizer.acc_stats = False
            
        self.optimizer.zero_grad()
        (value_loss * self.value_loss_coef + action_loss -
         dist_entropy * self.entropy_coef + kl_loss * self.value_loss_coef).backward()
       

        if self.acktr == False:
            nn.utils.clip_grad_norm_(self.actor_critic.parameters(),
                                     self.max_grad_norm)

        self.optimizer.step()

        return value_loss.item(), action_loss.item(), dist_entropy.item()

