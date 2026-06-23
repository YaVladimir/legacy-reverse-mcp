package ru.bank.deposit;

import org.springframework.stereotype.Repository;

@Repository
public class DepositRepository {
    public Deposit save(Deposit d) {
        return d;
    }

    public Deposit findById(Long id) {
        return new Deposit();
    }
}
