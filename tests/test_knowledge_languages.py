"""Tests for the six general-purpose languages added on top of Python/JS/TS:
Java, C#, Go, PHP, C, C++. Each snippet here is the exact one verified
against real tree-sitter output during design, not a fresh guess."""

from devmemory import knowledge

JAVA = '''
package com.example;
import java.util.List;
import com.example.utils.Helper;

public class Invoice {
    private int total;
    public int calculateTotal(int price, int qty) {
        return Helper.round(price * qty);
    }
    public void printInvoice() {
        calculateTotal(10, 2);
    }
}
'''

CSHARP = '''
using System;
using MyApp.Utils;

namespace MyApp {
    public class Invoice {
        public int CalculateTotal(int price, int qty) {
            return Helper.Round(price * qty);
        }
    }
}
'''

GO = '''
package main

import (
    "fmt"
    "myapp/utils"
)

func CalculateTotal(price int, qty int) int {
    return utils.Round(price * qty)
}

type Invoice struct {
    Total int
}

type Amount int

func (inv *Invoice) Print() {
    fmt.Println(CalculateTotal(10, 2))
}
'''

GO_SINGLE_IMPORT = '''
package main

import "fmt"

func main() {
    fmt.Println("hi")
}
'''

PHP = '''<?php
require_once 'Helper.php';
use App\\Utils\\Helper;

class Invoice {
    public function calculateTotal($price, $qty) {
        return Helper::round($price * $qty);
    }
}

function standaloneFunc($x) {
    return $x + 1;
}

function caller($y) {
    return standaloneFunc($y);
}
'''

C_SRC = '''
#include <stdio.h>
#include "helper.h"

int calculate_total(int price, int qty) {
    return round_price(price * qty);
}

struct Invoice {
    int total;
};

void print_invoice() {
    calculate_total(10, 2);
}
'''

CPP_SRC = '''
#include <iostream>
#include "helper.h"

class Invoice {
public:
    int calculateTotal(int price, int qty) {
        return Helper::round(price * qty);
    }
};

void printInvoice() {
    Invoice inv;
    inv.calculateTotal(10, 2);
}
'''


def _write(root, rel_path, content):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_java_imports_definitions_and_calls(tmp_path):
    path = _write(tmp_path, "Invoice.java", JAVA)
    parsed = knowledge.parse_file(path, "java")

    assert "java.util.List" in parsed.imports
    assert "com.example.utils.Helper" in parsed.imports
    names = {(d.kind, d.name) for d in parsed.definitions}
    assert ("class", "Invoice") in names
    assert ("method", "calculateTotal") in names
    assert ("method", "printInvoice") in names
    assert any(callee == "round" for _, callee in parsed.calls)
    assert any(callee == "calculateTotal" for _, callee in parsed.calls)


def test_csharp_imports_definitions_and_calls(tmp_path):
    path = _write(tmp_path, "Invoice.cs", CSHARP)
    parsed = knowledge.parse_file(path, "csharp")

    assert "System" in parsed.imports
    assert "MyApp.Utils" in parsed.imports
    names = {(d.kind, d.name) for d in parsed.definitions}
    assert ("class", "Invoice") in names
    assert ("method", "CalculateTotal") in names
    assert any(callee == "Round" for _, callee in parsed.calls)


def test_go_imports_definitions_and_calls(tmp_path):
    path = _write(tmp_path, "main.go", GO)
    parsed = knowledge.parse_file(path, "go")

    assert "fmt" in parsed.imports
    assert "myapp/utils" in parsed.imports
    names = {(d.kind, d.name) for d in parsed.definitions}
    assert ("function", "CalculateTotal") in names
    assert ("method", "Print") in names
    assert ("class", "Invoice") in names  # struct, surfaced as our "class" kind
    assert ("class", "Amount") not in names  # plain type alias, not a struct -- correctly excluded
    assert any(callee == "Round" for _, callee in parsed.calls)
    assert any(callee == "Println" for _, callee in parsed.calls)
    assert any(callee == "CalculateTotal" for _, callee in parsed.calls)


def test_go_single_import_form_without_parens(tmp_path):
    path = _write(tmp_path, "main.go", GO_SINGLE_IMPORT)
    parsed = knowledge.parse_file(path, "go")
    assert "fmt" in parsed.imports


def test_php_imports_definitions_and_three_call_forms(tmp_path):
    path = _write(tmp_path, "Invoice.php", PHP)
    parsed = knowledge.parse_file(path, "php")

    assert any("Helper" in i for i in parsed.imports)
    names = {(d.kind, d.name) for d in parsed.definitions}
    assert ("class", "Invoice") in names
    assert ("method", "calculateTotal") in names
    assert ("function", "standaloneFunc") in names
    assert ("function", "caller") in names
    assert any(callee == "round" for _, callee in parsed.calls)  # scoped_call_expression
    assert any(callee == "standaloneFunc" for _, callee in parsed.calls)  # function_call_expression


def test_c_imports_definitions_and_calls(tmp_path):
    path = _write(tmp_path, "invoice.c", C_SRC)
    parsed = knowledge.parse_file(path, "c")

    assert "./helper.h" in parsed.imports
    assert "stdio.h" in parsed.imports
    names = {(d.kind, d.name) for d in parsed.definitions}
    assert ("function", "calculate_total") in names
    assert ("function", "print_invoice") in names
    assert ("class", "Invoice") in names  # struct
    calculate_total = next(d for d in parsed.definitions if d.name == "calculate_total")
    assert calculate_total.signature == "(int price, int qty)"
    assert any(callee == "round_price" for _, callee in parsed.calls)
    assert any(callee == "calculate_total" for _, callee in parsed.calls)


def test_cpp_imports_definitions_and_calls(tmp_path):
    path = _write(tmp_path, "invoice.cpp", CPP_SRC)
    parsed = knowledge.parse_file(path, "cpp")

    assert "./helper.h" in parsed.imports
    assert "iostream" in parsed.imports
    names = {(d.kind, d.name) for d in parsed.definitions}
    assert ("class", "Invoice") in names
    assert ("function", "calculateTotal") in names
    assert ("function", "printInvoice") in names
    assert any(callee == "round" for _, callee in parsed.calls)  # Helper::round -> "round"


def test_c_quoted_include_resolves_to_local_file_via_scan(git_repo):
    import subprocess
    _write(git_repo, "invoice.c", C_SRC)
    _write(git_repo, "helper.h", "int round_price(int x);\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c files"], cwd=git_repo, check=True)

    knowledge.scan(git_repo)
    result = knowledge.inspect(git_repo, file="invoice.c")
    targets = {e["file"] for e in result["outgoing"] if e["edge_kind"] == "imports"}
    assert "helper.h" in targets


def test_java_external_only_import_resolution_via_scan(git_repo):
    import subprocess
    _write(git_repo, "Invoice.java", JAVA)
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "java file"], cwd=git_repo, check=True)

    knowledge.scan(git_repo)
    result = knowledge.inspect(git_repo, file="Invoice.java")
    external_targets = {
        e["file"] for e in result["outgoing"]
        if e["edge_kind"] == "imports" and e["kind"] == "external"
    }
    assert "java.util.List" in external_targets
    assert "com.example.utils.Helper" in external_targets
