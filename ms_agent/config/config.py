# Copyright (c) Alibaba, Inc. and its affiliates.
# 导入必要的模块
import argparse  # 用于解析命令行参数
import os.path  # 用于处理文件路径
from abc import abstractmethod  # 抽象方法装饰器
from copy import deepcopy  # 深拷贝功能
from typing import Any, Dict, Union  # 类型注解

# 导入项目内部模块
from ms_agent.utils import get_logger  # 获取日志记录器
from omegaconf import DictConfig, ListConfig, OmegaConf  # OmegaConf配置管理库
from omegaconf.basecontainer import BaseContainer  # OmegaConf基础容器类

from modelscope import snapshot_download  # ModelScope模型下载功能
from .env import Env  # 环境变量处理模块

# 获取日志记录器实例
logger = get_logger()


class ConfigLifecycleHandler:
    """配置生命周期处理器
    
    用于在任务开始和结束时修改配置，支持多个代理的配置管理
    """

    def task_begin(self, config: DictConfig, tag: str) -> DictConfig:
        """任务开始时修改配置

        Args:
            config(`DictConfig`): 配置实例
            tag(`str`): 代理标签，可以在一个处理器中处理多个代理的配置

        Returns:
            `DictConfig`: 修改后的配置

        """
        return config

    def task_end(self, config: DictConfig, tag: str) -> DictConfig:
        """任务结束时修改配置，配置将传递给工作流中的下一个代理

        如果下一个代理有自己的配置，此函数将不起作用。

        Args:
            config(`DictConfig`): 配置实例
            tag(`str`): 代理标签，可以在一个处理器中处理多个代理的配置

        Returns:
            `DictConfig`: 修改后的配置
        """
        return config


class Config:
    """配置类 - 所有任务都从配置开始
    
    负责加载、解析和管理配置文件，支持多种配置文件格式
    """

    tag: str = ''  # 配置标签
    # 支持的配置文件名列表
    supported_config_names = [
        'workflow.yaml', 'workflow.yml', 'agent.yaml', 'agent.yml'
    ]

    @classmethod
    def from_task(cls,
                  config_dir_or_id: str,
                  env: Dict[str, str] = None) -> Union[DictConfig, ListConfig]:
        """从任务目录或ModelScope仓库ID读取配置文件并返回配置对象

        Args:
            config_dir_or_id: 本地任务目录路径或ModelScope仓库中的ID
            env: 额外的环境变量，除了已经包含在环境中或`.env`文件中的变量

        Returns:
            配置对象
        """
        # 如果路径不存在，尝试从ModelScope下载
        if not os.path.exists(config_dir_or_id):
            config_dir_or_id = snapshot_download(config_dir_or_id)

        config = None
        name = None
        
        # 如果是文件路径，直接加载
        if os.path.isfile(config_dir_or_id):
            config = OmegaConf.load(config_dir_or_id)
            name = os.path.basename(config_dir_or_id)
            config_dir_or_id = os.path.dirname(config_dir_or_id)
        else:
            # 如果是目录，查找支持的配置文件
            for _name in Config.supported_config_names:
                config_file = os.path.join(config_dir_or_id, _name)
                if os.path.exists(config_file):
                    config = OmegaConf.load(config_file)
                    name = _name
                    break

        # 确保找到了有效的配置文件
        assert config is not None, (
            f'Cannot find any valid config file in {config_dir_or_id}, '
            f'supported configs are: {Config.supported_config_names}')
        
        # 加载环境变量并更新配置
        envs = Env.load_env(env)
        cls._update_config(config, envs)
        
        # 解析命令行参数并更新配置
        _dict_config = cls.parse_args()
        cls._update_config(config, _dict_config)
        
        # 设置配置的本地目录和名称
        config.local_dir = config_dir_or_id
        config.name = name
        
        # 填充缺失的字段
        config = cls.fill_missing_fields(config)
        return config

    @staticmethod
    def fill_missing_fields(config: DictConfig) -> DictConfig:
        """填充配置中缺失的必要字段
        
        Args:
            config: 配置对象
            
        Returns:
            填充后的配置对象
        """
        # 如果没有tools字段或为None，创建空的DictConfig
        if not hasattr(config, 'tools') or config.tools is None:
            config.tools = DictConfig({})
        # 如果没有callbacks字段或为None，创建空的ListConfig
        if not hasattr(config, 'callbacks') or config.callbacks is None:
            config.callbacks = ListConfig([])
        return config

    @staticmethod
    def is_workflow(config: DictConfig) -> bool:
        """判断配置是否为工作流配置
        
        Args:
            config: 配置对象
            
        Returns:
            如果是工作流配置返回True，否则返回False
        """
        assert config.name is not None, 'Cannot find a valid name in this config'
        return config.name in ['workflow.yaml', 'workflow.yml']

    @staticmethod
    def parse_args() -> Dict[str, Any]:
        """解析命令行参数
        
        解析形如 --key value 的命令行参数对
        
        Returns:
            解析后的参数字典
        """
        arg_parser = argparse.ArgumentParser()
        args, unknown = arg_parser.parse_known_args()
        _dict_config = {}
        
        # 处理未知参数，期望格式为 --key value
        if unknown:
            for idx in range(1, len(unknown) - 1, 2):
                key = unknown[idx]
                value = unknown[idx + 1]
                # 确保参数以--开头
                assert key.startswith(
                    '--'), f'Parameter not correct: {unknown}'
                # 去掉--前缀作为键
                _dict_config[key[2:]] = value
        return _dict_config

    @staticmethod
    def _update_config(config: Union[DictConfig, ListConfig],
                       extra: Dict[str, str] = None):
        """使用额外的参数更新配置
        
        支持两种替换模式：
        1. 直接键匹配：如果extra中有对应的键，直接替换
        2. 占位符替换：如果值是<key>格式且key在extra中，进行替换
        
        Args:
            config: 要更新的配置对象
            extra: 额外的参数字典
        """
        if not extra:
            return config

        def traverse_config(_config: Union[DictConfig, ListConfig, Any]):
            """递归遍历配置结构进行更新"""
            if isinstance(_config, DictConfig):
                # 处理字典配置
                for name, value in _config.items():
                    if isinstance(value, BaseContainer):
                        # 如果值是容器类型，递归处理
                        traverse_config(value)
                    else:
                        # 直接键匹配替换
                        if name in extra:
                            logger.info(f'Replacing {name} with extra value.')
                            setattr(_config, name, extra[name])
                        # 占位符替换：<key>格式
                        if (isinstance(value, str) and value.startswith('<')
                                and value.endswith('>')
                                and value[1:-1] in extra):
                            logger.info(f'Replacing {value} with extra value.')
                            setattr(_config, name, extra[name])

            elif isinstance(_config, ListConfig):
                # 处理列表配置
                for idx in range(len(_config)):
                    value = _config[idx]
                    if isinstance(value, BaseContainer):
                        # 如果值是容器类型，递归处理
                        traverse_config(value)
                    else:
                        # 占位符替换：<key>格式
                        if (isinstance(value, str) and value.startswith('<')
                                and value.endswith('>')
                                and value[1:-1] in extra):
                            logger.info(f'Replacing {value} with extra value.')
                            _config[idx] = extra[value[1:-1]]

        # 开始遍历配置
        traverse_config(config)
        return None

    @staticmethod
    def convert_mcp_servers_to_json(
            config: Union[DictConfig,
                          ListConfig]) -> Dict[str, Dict[str, Any]]:
        """将MCP服务器配置转换为JSON格式的MCP配置
        
        Args:
            config: 配置对象
            
        Returns:
            JSON格式的MCP服务器配置字典
        """
        servers = {'mcpServers': {}}
        
        # 检查配置中是否有tools字段
        if getattr(config, 'tools', None):
            # 遍历所有工具配置
            for server, server_config in config.tools.items():
                # 如果工具配置标记为MCP服务器（默认为True）
                if getattr(server_config, 'mcp', True):
                    # 深拷贝服务器配置到MCP服务器字典中
                    servers['mcpServers'][server] = deepcopy(server_config)
        return servers
