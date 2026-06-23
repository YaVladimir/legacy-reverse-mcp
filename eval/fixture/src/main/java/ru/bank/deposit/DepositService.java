package ru.bank.deposit;

import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.scheduling.annotation.Scheduled;

@Service
public class DepositService {
    @Autowired
    private DepositRepository repo;

    public Deposit create(DepositRequest req) {
        return repo.save(new Deposit());
    }

    public Deposit find(Long id) {
        return repo.findById(id);
    }

    @Scheduled(fixedRate = 60000)
    public void sweep() {
    }
}
