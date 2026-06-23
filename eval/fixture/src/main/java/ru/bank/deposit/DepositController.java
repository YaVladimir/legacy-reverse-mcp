package ru.bank.deposit;

import org.springframework.web.bind.annotation.*;
import lombok.RequiredArgsConstructor;

@RestController
@RequestMapping("/deposits")
@RequiredArgsConstructor
public class DepositController {
    private final DepositService depositService;

    @PostMapping("/create")
    public Deposit createDeposit(@RequestBody DepositRequest req) {
        return depositService.create(req);
    }

    @GetMapping("/{id}")
    public Deposit get(@PathVariable Long id) {
        return depositService.find(id);
    }
}
