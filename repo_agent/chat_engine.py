import os, json
import re, sys
from openai import BadRequestError, OpenAI
from openai import APIConnectionError
import tiktoken
import time
from config import language_mapping
from project_manager import ProjectManager
from prompt import SYS_PROMPT, USR_PROMPT
from doc_meta_info import DocItem
import inspect
from collections import defaultdict
from repo_agent.log import logger


def get_import_statements():
    source_lines = inspect.getsourcelines(sys.modules[__name__])[0]
    import_lines = [
        line
        for line in source_lines
        if line.strip().startswith("import") or line.strip().startswith("from")
    ]
    return import_lines


def build_path_tree(who_reference_me, reference_who, doc_item_path):
    def tree():
        return defaultdict(tree)

    path_tree = tree()

    for path_list in [who_reference_me, reference_who]:
        for path in path_list:
            parts = path.split(os.sep)
            node = path_tree
            for part in parts:
                node = node[part]

    # 处理 doc_item_path
    parts = doc_item_path.split(os.sep)
    parts[-1] = "✳️" + parts[-1]  # 在最后一个对象前面加上星号
    node = path_tree
    for part in parts:
        node = node[part]

    def tree_to_string(tree, indent=0):
        s = ""
        for key, value in sorted(tree.items()):
            s += "    " * indent + key + "\n"
            if isinstance(value, dict):
                s += tree_to_string(value, indent + 1)
        return s

    return tree_to_string(path_tree)


class ChatEngine:
    """
    ChatEngine is used to generate the doc of functions or classes.
    """

    def __init__(self, CONFIG):
        self.config = CONFIG

    def num_tokens_from_string(self, string: str, encoding_name="cl100k_base") -> int:
        """Returns the number of tokens in a text string."""
        encoding = tiktoken.get_encoding(encoding_name)
        num_tokens = len(encoding.encode(string))
        return num_tokens

    def generate_doc(self, doc_item: DocItem, file_handler):
        code_info = doc_item.content
        referenced = len(doc_item.who_reference_me) > 0

        # print("len(referencer):\n",len(referencer))

        # def get_code_from_json(json_file, referencer):
        #     '''根据给出的referencer，找出其源码
        #     '''
        #     with open(json_file, 'r', encoding='utf-8') as f:
        #         data = json.load(f)

        #     code_from_referencer = {}
        #     for ref in referencer:
        #         file_path, line_number, _ = ref
        #         if file_path in data:
        #             objects = data[file_path]
        #             min_obj = None
        #             for obj_name, obj in objects.items():
        #                 if obj['code_start_line'] <= line_number <= obj['code_end_line']:
        #                     if min_obj is None or (obj['code_end_line'] - obj['code_start_line'] < min_obj['code_end_line'] - min_obj['code_start_line']):
        #                         min_obj = obj
        #             if min_obj is not None:
        #                 if file_path not in code_from_referencer:
        #                     code_from_referencer[file_path] = []
        #                 code_from_referencer[file_path].append(min_obj['code_content'])
        #     return code_from_referencer

        code_type = code_info["type"]
        code_name = code_info["name"]
        code_content = code_info["code_content"]
        have_return = code_info["have_return"]
        who_reference_me = doc_item.who_reference_me_name_list
        reference_who = doc_item.reference_who_name_list
        file_path = doc_item.get_full_name()
        doc_item_path = file_path + "/" + code_name

        # 树结构路径通过全局信息中的who reference me 和 reference who + 自身的file_path来获取
        project_structure = build_path_tree(
            who_reference_me, reference_who, doc_item_path
        )

        # project_manager = ProjectManager(repo_path=file_handler.repo_path, project_hierarchy=file_handler.project_hierarchy)
        # project_structure = project_manager.get_project_structure()
        # file_path = os.path.join(file_handler.repo_path, file_handler.file_path)
        # code_from_referencer = get_code_from_json(project_manager.project_hierarchy, referencer) #
        # referenced = True if len(code_from_referencer) > 0 else False
        # referencer_content = '\n'.join([f'File_Path:{file_path}\n' + '\n'.join([f'Corresponding code as follows:\n{code}\n[End of this part of code]' for code in codes]) + f'\n[End of {file_path}]' for file_path, codes in code_from_referencer.items()])

        def get_referenced_prompt(doc_item: DocItem) -> str:
            if len(doc_item.reference_who) == 0:
                return ""
            prompt = [
                """As you can see, the code calls the following objects, their code and docs are as following:"""
            ]
            for k, reference_item in enumerate(doc_item.reference_who):
                instance_prompt = (
                    f"""obj: {reference_item.get_full_name()}\nDocument: \n{reference_item.md_content[-1] if len(reference_item.md_content) > 0 else 'None'}\nRaw code:```\n{reference_item.content['code_content'] if 'code_content' in reference_item.content.keys() else ''}\n```"""
                    + "=" * 10
                )
                prompt.append(instance_prompt)
            return "\n".join(prompt)

        def get_referencer_prompt(doc_item: DocItem) -> str:
            if len(doc_item.who_reference_me) == 0:
                return ""
            prompt = [
                """Also, the code has been called by the following objects, their code and docs are as following:"""
            ]
            for k, referencer_item in enumerate(doc_item.who_reference_me):
                instance_prompt = (
                    f"""obj: {referencer_item.get_full_name()}\nDocument: \n{referencer_item.md_content[-1] if len(referencer_item.md_content) > 0 else 'None'}\nRaw code:```\n{referencer_item.content['code_content'] if 'code_content' in referencer_item.content.keys() else 'None'}\n```"""
                    + "=" * 10
                )
                prompt.append(instance_prompt)
            return "\n".join(prompt)

        def get_relationship_description(referencer_content, reference_letter):
            if referencer_content and reference_letter:
                has_relationship = "And please include the reference relationship with its callers and callees in the project from a functional perspective"
            elif referencer_content:
                return "And please include the relationship with its callers in the project from a functional perspective."
            elif reference_letter:
                return "And please include the relationship with its callees in the project from a functional perspective."
            else:
                return ""

        # language
        language = self.config["language"]
        if language not in language_mapping:
            raise KeyError(
                f"Language code {language} is not given! Supported languages are: {json.dumps(language_mapping)}"
            )

        language = language_mapping[language]

        code_type_tell = "Class" if code_type == "ClassDef" else "Function"
        parameters_or_attribute = (
            "attributes" if code_type == "ClassDef" else "parameters"
        )
        have_return_tell = (
            "**Output Example**: Mock up a possible appearance of the code's return value."
            if have_return
            else ""
        )
        # reference_letter = "This object is called in the following files, the file paths and corresponding calling parts of the code are as follows:" if referenced else ""
        combine_ref_situation = (
            "and combine it with its calling situation in the project,"
            if referenced
            else ""
        )

        referencer_content = get_referencer_prompt(doc_item)
        reference_letter = get_referenced_prompt(doc_item)
        has_relationship = get_relationship_description(
            referencer_content, reference_letter
        )

        project_structure_prefix = ", and the related hierarchical structure of this project is as follows (The current object is marked with an *):"

        sys_prompt = SYS_PROMPT.format(
            combine_ref_situation=combine_ref_situation,
            file_path=file_path,
            project_structure_prefix=project_structure_prefix,
            project_structure=project_structure,
            code_type_tell=code_type_tell,
            code_name=code_name,
            code_content=code_content,
            have_return_tell=have_return_tell,
            # referenced=referenced,
            has_relationship=has_relationship,
            reference_letter=reference_letter,
            referencer_content=referencer_content,
            parameters_or_attribute=parameters_or_attribute,
            language=language,
        )

        usr_prompt = USR_PROMPT.format(language=language)
        # import pdb; pdb.set_trace()
        # print("\nsys_prompt:\n",sys_prompt)
        # print("\nusr_prompt:\n",str(usr_prompt))

        # # 保存prompt到txt文件
        # with open(f'prompt_output/sys_prompt_{code_name}.txt', 'w', encoding='utf-8') as f:
        #     f.write(sys_prompt+'\n'+ usr_prompt)

        max_attempts = 5  # 设置最大尝试次数
        model = self.config["default_completion_kwargs"]["model"]
        code_max_length = 8192 - 1024 - 1
        if model == "gpt-3.5-turbo":
            code_max_length = 4096 - 1024 - 1
        # 检查tokens长度
        if (
            self.num_tokens_from_string(sys_prompt)
            + self.num_tokens_from_string(usr_prompt)
            >= code_max_length
        ):
            print("The code is too long, using gpt-3.5-turbo-16k to process it.")
            model = "gpt-3.5-turbo-16k"

        attempt = 0
        while attempt < max_attempts:
            try:
                # 获取基本配置
                client = OpenAI(
                    api_key=self.config["api_keys"][model][0]["api_key"],
                    base_url=self.config["api_keys"][model][0]["base_url"],
                    timeout=self.config["default_completion_kwargs"]["request_timeout"],
                )

                messages = [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": usr_prompt},
                ]
                # print(f"tokens of system-prompt={self.num_tokens_from_string(sys_prompt)}, user-prompt={self.num_tokens_from_string(usr_prompt)}")
                # print(f"message:\n{messages}\n")

                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=self.config["default_completion_kwargs"]["temperature"],
                    max_tokens=1024,
                )

                response_message = response.choices[0].message

                # 如果 response_message 是 None，则继续下一次循环
                if response_message is None:
                    attempt += 1
                    continue

                # print(f"\nAnswer:\n{response_message.content}\n")

                return response_message

            except APIConnectionError as e:
                print(f"Connection error: {e}. Attempt {attempt + 1} of {max_attempts}")
                # Retry after 7 seconds
                time.sleep(7)
                attempt += 1
                if attempt == max_attempts:
                    raise
                else:
                    continue  # Try to request again

            except BadRequestError as e:
                # import pdb; pdb.set_trace()
                if "context_length_exceeded" in str(e):
                    logger.info(
                        f"Error: The model's maximum context length is exceeded. Reducing the length of the messages. Attempt {attempt + 1} of {max_attempts}"
                    )
                    logger.info(
                        f"Length of sys_prompt: {len(sys_prompt)}, removing project_structure..."
                    )
                    project_structure_prefix = ""
                    project_structure = ""
                    # Remove project_structure and project_structure_prefix
                    sys_prompt = SYS_PROMPT.format(
                        reference_letter=reference_letter,
                        combine_ref_situation=combine_ref_situation,
                        file_path=file_path,
                        project_structure_prefix="",
                        project_structure="",
                        code_type_tell=code_type_tell,
                        code_name=code_name,
                        code_content=code_content,
                        have_return_tell=have_return_tell,
                        has_relationship=has_relationship,
                        referenced=referenced,
                        referencer_content=referencer_content,
                        parameters_or_attribute=parameters_or_attribute,
                        language=language,
                    )

                    attempt += 1
                    if attempt >= 2:
                        # Remove related callers and callees
                        logger.info(
                            f"Length of sys_prompt: {len(sys_prompt)}, removing related callers and callees..."
                        )
                        referenced = False
                        referencer_content = ""
                        reference_letter = ""
                        combine_ref_situation = ""

                        sys_prompt = SYS_PROMPT.format(
                            combine_ref_situation="",
                            file_path=file_path,
                            project_structure_prefix=project_structure_prefix,
                            project_structure=project_structure,
                            code_type_tell=code_type_tell,
                            code_name=code_name,
                            code_content=code_content,
                            have_return_tell=have_return_tell,
                            # referenced=referenced,
                            has_relationship=has_relationship,
                            reference_letter="",
                            referencer_content="",
                            parameters_or_attribute=parameters_or_attribute,
                            language=language,
                        )

                    continue  # Try to request again
                else:
                    print(
                        f"An OpenAI error occurred: {e}. Attempt {attempt + 1} of {max_attempts}"
                    )

            except Exception as e:
                print(
                    f"An unknown error occurred: {e}. Attempt {attempt + 1} of {max_attempts}"
                )
                # Retry after 10 seconds
                time.sleep(10)
                attempt += 1
                if attempt == max_attempts:
                    return None
