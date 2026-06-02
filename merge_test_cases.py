#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将三个章节的黑盒测试用例Markdown文件合并为一个Excel文件
支持两种格式：
  - 第一章：两个表格（项目/内容表 + 步骤/操作详情/预期结果表）
  - 第二三章：单个表格（项目/内容表，包含测试步骤和预期结果）
"""

import re
import os
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill


def parse_test_cases(filepath, chapter_name):
    """解析Markdown文件中的测试用例，支持两种表格格式"""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    test_cases = []

    # 按 ### 标题分割，每个标题对应一个测试用例
    # 找到所有 ### 标题及其后的内容
    heading_pattern = r'###\s+([^\s：:][^：:]*?)\s*[：:]\s*(.+?)(?:\n|$)'
    headings = list(re.finditer(heading_pattern, content))

    for i, heading in enumerate(headings):
        case_id_from_title = heading.group(1).strip()
        case_name_from_title = heading.group(2).strip()

        # 获取该标题到下一个标题(或---)之间的内容
        start_pos = heading.end()
        if i + 1 < len(headings):
            end_pos = headings[i + 1].start()
        else:
            end_pos = len(content)

        block = content[start_pos:end_pos]

        # 初始化用例数据
        case_data = {
            '章节': chapter_name,
            '用例编号': case_id_from_title,
            '用例名称': case_name_from_title,
            '前置条件': '',
            '测试步骤': '',
            '预期结果': '',
            '优先级': '',
            '测试类型': '',
        }

        # 提取所有表格
        tables = re.findall(r'\|.*?\n\|[-\s|]+\n((?:\|.*\n)+)', block)

        for table_content in tables:
            lines = table_content.strip().split('\n')
            first_data_line = lines[0].strip() if lines else ''

            # 判断表格类型：检查第一行的列数和内容
            parts_count = len([p for p in first_data_line.split('|') if p.strip()])

            if parts_count == 2:
                # 项目/内容 两列表格（可能包含所有字段或只包含部分字段）
                for line in lines:
                    line = line.strip()
                    if not line.startswith('|'):
                        continue
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) >= 3:
                        key = parts[1].replace('**', '').strip()
                        value = parts[2].replace('**', '').strip()
                        if key in case_data:
                            case_data[key] = value

            elif parts_count == 3:
                # 步骤/操作详情/预期结果 三列表格
                steps_list = []
                expected_list = []

                for line in lines:
                    line = line.strip()
                    if not line.startswith('|'):
                        continue
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) >= 4:
                        step_num = parts[1].replace('**', '').strip()
                        operation = parts[2].replace('**', '').strip()
                        expected = parts[3].replace('**', '').strip()
                        if step_num and operation:
                            steps_list.append(f"步骤{step_num}：{operation}")
                            if expected:
                                expected_list.append(f"步骤{step_num}：{expected}")

                if steps_list:
                    case_data['测试步骤'] = '; '.join(steps_list)
                if expected_list:
                    case_data['预期结果'] = '; '.join(expected_list)

        test_cases.append(case_data)

    return test_cases


def main():
    base_dir = '/Users/apple/Documents/ceshi'

    files = [
        ('第一章：对账文件获取与解析', os.path.join(base_dir, '黑盒测试用例_第一章.md')),
        ('第二章：对账执行引擎', os.path.join(base_dir, '黑盒测试用例_第二章.md')),
        ('第三章：对账结果展示与差异处理', os.path.join(base_dir, '黑盒测试用例_第三章.md')),
    ]

    all_cases = []
    for chapter_name, filepath in files:
        if os.path.exists(filepath):
            cases = parse_test_cases(filepath, chapter_name)
            all_cases.extend(cases)
            print(f"从 {chapter_name} 解析到 {len(cases)} 条测试用例")
        else:
            print(f"文件不存在: {filepath}")

    if not all_cases:
        print("未解析到任何测试用例，退出")
        return

    # 创建Excel
    wb = Workbook()
    ws = wb.active
    ws.title = "测试用例汇总"

    # 表头
    headers = ['序号', '章节', '用例编号', '用例名称', '前置条件', '测试步骤', '预期结果', '优先级', '测试类型']

    # 样式定义
    header_font = Font(name='微软雅黑', bold=True, size=11, color='FFFFFF')
    header_fill = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
    header_alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    cell_font = Font(name='微软雅黑', size=10)
    cell_alignment = Alignment(vertical='top', wrap_text=True)
    center_alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    thin_border = Border(
        left=Side(style='thin', color='D9D9D9'),
        right=Side(style='thin', color='D9D9D9'),
        top=Side(style='thin', color='D9D9D9'),
        bottom=Side(style='thin', color='D9D9D9'),
    )

    # 优先级颜色
    priority_fills = {
        'P0': PatternFill(start_color='FCE4EC', end_color='FCE4EC', fill_type='solid'),
        'P1': PatternFill(start_color='FFF3E0', end_color='FFF3E0', fill_type='solid'),
        'P2': PatternFill(start_color='E8F5E9', end_color='E8F5E9', fill_type='solid'),
    }

    # 章节交替颜色
    chapter_fills = {
        '第一章：对账文件获取与解析': PatternFill(start_color='EBF5FB', end_color='EBF5FB', fill_type='solid'),
        '第二章：对账执行引擎': PatternFill(start_color='FEF9E7', end_color='FEF9E7', fill_type='solid'),
        '第三章：对账结果展示与差异处理': PatternFill(start_color='E8F8F5', end_color='E8F8F5', fill_type='solid'),
    }

    # 写入表头
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = thin_border

    # 写入数据
    for row_idx, case in enumerate(all_cases, 2):
        values = [
            row_idx - 1,
            case['章节'],
            case['用例编号'],
            case['用例名称'],
            case['前置条件'],
            case['测试步骤'],
            case['预期结果'],
            case['优先级'],
            case['测试类型'],
        ]

        for col_idx, value in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = cell_font
            cell.border = thin_border

            # 序号、章节、优先级、测试类型居中
            if col_idx in (1, 2, 8, 9):
                cell.alignment = center_alignment
            else:
                cell.alignment = cell_alignment

        # 优先级颜色标记
        priority = case['优先级']
        if priority in priority_fills:
            ws.cell(row=row_idx, column=8).fill = priority_fills[priority]

        # 章节背景色（仅在序号列标记）
        chapter = case['章节']
        if chapter in chapter_fills:
            ws.cell(row=row_idx, column=2).fill = chapter_fills[chapter]

    # 设置列宽
    col_widths = {
        'A': 6,    # 序号
        'B': 22,   # 章节
        'C': 18,   # 用例编号
        'D': 40,   # 用例名称
        'E': 50,   # 前置条件
        'F': 60,   # 测试步骤
        'G': 60,   # 预期结果
        'H': 8,    # 优先级
        'I': 10,   # 测试类型
    }
    for col_letter, width in col_widths.items():
        ws.column_dimensions[col_letter].width = width

    # 冻结首行
    ws.freeze_panes = 'A2'

    # 设置自动筛选
    ws.auto_filter.ref = f"A1:I{len(all_cases) + 1}"

    # 输出文件
    output_path = os.path.join(base_dir, '渠道对账系统_黑盒测试用例_合并.xlsx')
    wb.save(output_path)
    print(f"\nExcel文件已生成: {output_path}")
    print(f"共合并 {len(all_cases)} 条测试用例")


if __name__ == '__main__':
    main()