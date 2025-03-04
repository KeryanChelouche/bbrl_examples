import sys
import os
import copy

import torch
import torch.nn as nn
import gym
import bbrl_gym
import hydra

from omegaconf import DictConfig
from bbrl import get_arguments, get_class
from bbrl.workspace import Workspace
from bbrl.agents import Agents, TemporalAgent

from bbrl.utils.functionalb import gae

from bbrl.visu.visu_policies import plot_policy
from bbrl.visu.visu_critics import plot_critic

from bbrl_examples.models.critics import VAgent
from bbrl_examples.models.actors import TunableVarianceContinuousActor
from bbrl_examples.models.actors import DiscreteActor
from bbrl_examples.models.loggers import Logger

from bbrl_examples.models.envs import create_env_agents

# HYDRA_FULL_ERROR = 1

import matplotlib

matplotlib.use("TkAgg")


# Create the PPO Agent
def create_ppo_agent(cfg, train_env_agent, eval_env_agent):
    obs_size, act_size = train_env_agent.get_obs_and_actions_sizes()
    if train_env_agent.is_continuous_action():
        action_agent = TunableVarianceContinuousActor(
            obs_size, cfg.algorithm.architecture.actor_hidden_size, act_size
        )
    else:
        action_agent = DiscreteActor(
            obs_size, cfg.algorithm.architecture.actor_hidden_size, act_size
        )
    tr_agent = Agents(train_env_agent, action_agent)
    ev_agent = Agents(eval_env_agent, action_agent)

    critic_agent = TemporalAgent(
        VAgent(obs_size, cfg.algorithm.architecture.critic_hidden_size)
    )

    train_agent = TemporalAgent(tr_agent)
    eval_agent = TemporalAgent(ev_agent)
    train_agent.seed(cfg.algorithm.seed)

    old_policy = copy.deepcopy(action_agent)
    old_critic_agent = copy.deepcopy(critic_agent)
    return train_agent, eval_agent, critic_agent, old_policy, old_critic_agent


def make_gym_env(env_name):
    env = gym.make(env_name)
    # print("obs:", env.observation_space)
    # print("act:", env.action_space)
    return env


# Configure the optimizer
def setup_optimizer(cfg, action_agent, critic_agent):
    optimizer_args = get_arguments(cfg.optimizer)
    parameters = nn.Sequential(action_agent, critic_agent).parameters()
    optimizer = get_class(cfg.optimizer)(parameters, **optimizer_args)
    return optimizer


def compute_advantage_loss(cfg, reward, must_bootstrap, v_value):
    # Compute temporal difference with GAE
    advantages = gae(
        v_value,
        reward,
        must_bootstrap,
        cfg.algorithm.discount_factor,
        cfg.algorithm.gae,
    )
    # Compute critic loss
    td_error = advantages**2
    critic_loss = td_error.mean()
    return critic_loss, advantages


def compute_actor_loss(advantages, ratio, clip_range):
    actor_loss_1 = advantages * ratio
    actor_loss_2 = advantages * torch.clamp(ratio, 1 - clip_range, 1 + clip_range)
    actor_loss = torch.min(actor_loss_1, actor_loss_2).mean()
    return actor_loss


def run_ppo(cfg):
    # 1)  Build the  logger
    logger = Logger(cfg)
    best_reward = -10e9

    # 2) Create the environment agent
    train_env_agent, eval_env_agent = create_env_agents(cfg)

    (
        train_agent,
        eval_agent,
        critic_agent,
        old_policy,
        old_critic_agent,
    ) = create_ppo_agent(cfg, train_env_agent, eval_env_agent)
    old_train_agent = TemporalAgent(old_policy)
    train_workspace = Workspace()

    # Configure the optimizer
    optimizer = setup_optimizer(cfg, train_agent, critic_agent)
    nb_steps = 0
    tmp_steps = 0

    # Training loop
    for epoch in range(cfg.algorithm.max_epochs):
        # Execute the agent in the workspace
        if epoch > 0:
            train_workspace.zero_grad()
            train_workspace.copy_n_last_steps(1)
            train_agent(
                train_workspace,
                t=1,
                n_steps=cfg.algorithm.n_steps - 1,
                stochastic=True,
                predict_proba=False,
                compute_entropy=True,
            )
            old_train_agent(
                train_workspace,
                t=1,
                n_steps=cfg.algorithm.n_steps - 1,
                stochastic=True,
                predict_proba=True,
                compute_entropy=True,
            )
        else:
            train_agent(
                train_workspace,
                t=0,
                n_steps=cfg.algorithm.n_steps,
                stochastic=True,
                predict_proba=False,
                compute_entropy=True,
            )
            old_train_agent(
                train_workspace,
                t=0,
                n_steps=cfg.algorithm.n_steps,
                stochastic=True,
                predict_proba=True,
                compute_entropy=True,
            )

        # Compute the critic value over the whole workspace
        critic_agent(train_workspace, n_steps=cfg.algorithm.n_steps)

        transition_workspace = train_workspace.get_transitions()

        done, truncated, reward, action, action_logp, v_value = transition_workspace[
            "env/done",
            "env/truncated",
            "env/reward",
            "action",
            "action_logprobs",
            "v_value",
        ]

        nb_steps += action[0].shape[0]

        # Determines whether values of the critic should be propagated
        # True if the episode reached a time limit or if the task was not done
        # See https://colab.research.google.com/drive/1erLbRKvdkdDy0Zn1X_JhC01s1QAt4BBj?usp=sharing
        must_bootstrap = torch.logical_or(~done[1], truncated[1])

        with torch.no_grad():
            old_critic_agent(train_workspace, n_steps=cfg.algorithm.n_steps)
        old_action_logp = transition_workspace["logprob_predict"]
        old_v_value = transition_workspace["v_value"]

        act_diff = action_logp - old_action_logp
        ratios = act_diff.exp()
        ratios = ratios[:-1]
        # print("diff", act_diff)

        if cfg.algorithm.clip_range_vf > 0:
            # Clip the difference between old and new values
            # NOTE: this depends on the reward scaling
            v_value = old_v_value + torch.clamp(
                v_value - old_v_value,
                -cfg.algorithm.clip_range_vf,
                cfg.algorithm.clip_range_vf,
            )

        critic_loss, advantages = compute_advantage_loss(
            cfg, reward, must_bootstrap, v_value
        )

        actor_loss = compute_actor_loss(
            advantages.detach(), ratios, cfg.algorithm.clip_range
        )
        # actor_loss = (action_logp[:-1] * advantages.detach()).mean()

        # Entropy loss favor exploration
        entropy_loss = torch.mean(train_workspace["entropy"])

        # Store the losses for tensorboard display
        logger.log_losses(nb_steps, critic_loss, entropy_loss, actor_loss)

        loss = (
            cfg.algorithm.critic_coef * critic_loss
            - cfg.algorithm.actor_coef * actor_loss
            - cfg.algorithm.entropy_coef * entropy_loss
        )
        old_policy = copy.deepcopy(train_agent.agent.agents[1])
        old_train_agent = TemporalAgent(old_policy)
        old_critic_agent = copy.deepcopy(critic_agent)
        # Calculate approximate form of reverse KL Divergence for early stopping
        # see issue #417: https://github.com/DLR-RM/stable-baselines3/issues/417
        # and discussion in PR #419: https://github.com/DLR-RM/stable-baselines3/pull/419
        # and Schulman blog: http://joschu.net/blog/kl-approx.html
        """
        with torch.no_grad():
            log_ratio = log_prob - rollout_data.old_log_prob
            approx_kl_div = (
                torch.mean((torch.exp(log_ratio) - 1) - log_ratio).cpu().numpy()
            )
            approx_kl_divs.append(approx_kl_div)

        if cfg.algorithm.target_kl is not None and approx_kl_div > 1.5 * cfg.algorithm.target_kl:
            continue_training = False
            if cfg.logger.verbose == True:
                print(
                    f"Early stopping at step {epoch} due to reaching max kl: {approx_kl_div:.2f}"
                )
            break
        """
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(
            critic_agent.parameters(), cfg.algorithm.max_grad_norm
        )
        torch.nn.utils.clip_grad_norm_(
            train_agent.parameters(), cfg.algorithm.max_grad_norm
        )
        optimizer.step()

        if nb_steps - tmp_steps > cfg.algorithm.eval_interval:
            tmp_steps = nb_steps
            eval_workspace = Workspace()  # Used for evaluation
            eval_agent(
                eval_workspace,
                t=0,
                stop_variable="env/done",
                stochastic=True,
                predict_proba=False,
            )
            rewards = eval_workspace["env/cumulated_reward"][-1]
            mean = rewards.mean()
            logger.log_reward_losses(rewards, nb_steps)
            print(f"nb_steps: {nb_steps}, reward: {mean}")
            if cfg.save_best and mean > best_reward:
                best_reward = mean
                directory = "./ppo_agent/"
                if not os.path.exists(directory):
                    os.makedirs(directory)
                filename = (
                    directory
                    + cfg.gym_env.env_name
                    + "#ppo_basic#team#"
                    + str(mean.item())
                    + ".agt"
                )
                eval_agent.save_model(filename)
                if cfg.plot_agents:
                    plot_policy(
                        eval_agent.agent.agents[1],
                        eval_env_agent,
                        "./ppo_plots/",
                        cfg.gym_env.env_name,
                        best_reward,
                        stochastic=False,
                    )
                    plot_critic(
                        critic_agent.agent,
                        eval_env_agent,
                        "./ppo_plots/",
                        cfg.gym_env.env_name,
                        best_reward,
                    )


@hydra.main(
    config_path="./configs/",
    # config_name="ppo_lunarlander_continuous.yaml",
    config_name="ppo_swimmer.yaml",
    # config_name="ppo_pendulum.yaml",
    # config_name="ppo_cartpole.yaml",
    version_base="1.1",
)
def main(cfg: DictConfig):
    # print(OmegaConf.to_yaml(cfg))
    torch.manual_seed(cfg.algorithm.seed)
    run_ppo(cfg)


if __name__ == "__main__":
    sys.path.append(os.getcwd())
    main()
